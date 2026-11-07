#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Focused real-native song export, rollback and incremental-build checks."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(os.environ.get('HARNESS', ROOT / 'build/s12e10p1v1/harness')).resolve()
spec = importlib.util.spec_from_file_location('export_library', ROOT / 'tools/export_library.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class Exports(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jottune-song-export-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'web'
        self.catalog = self.root / 'catalog.json'
        self.sine = (ROOT / 'tests/golden/48000/sine.tl').read_text()
        header, events = self.sine.split('0 golden 0 on ', 1)
        (self.root / 'header.tl').write_text(header)
        (self.root / 'event parts.tl').write_text('0 golden 0 on ' + events)
        shutil.copyfile(ROOT / 'tests/golden/48000/bell.tl', self.root / 'bell.tl')
        self.value = {'version':1, 'songs':[{'id':'suite/one', 'title':'Fixture', 'collection':'Tests', 'revision':'first',
            'variants':[{'kind':'standard', 'id':'parted', 'paths':['header.tl','event parts.tl']},
                        {'kind':'keys', 'id':'bell', 'paths':['bell.tl']}]}]}
        self.save()

    def save(self):
        self.catalog.write_text(json.dumps(self.value))

    def export(self):
        return exporter.export_library(self.catalog, self.output, HARNESS, root=self.root, jobs=2)

    def snapshot(self):
        return {path.relative_to(self.output).as_posix():path.read_bytes()
                for path in self.output.rglob('*') if path.is_file()}

    def browser_manifest(self):
        """Synthetic serializer input, independent of the short native fixtures."""
        return {'version':1, 'songs':[{'id':'synthetic/one', 'title':'Synthetic song',
            'collection':'Synthetic collection', 'revision':'first', 'variants':[
                {'kind':kind, 'id':identifier, 'path':'songs/synthetic-' + kind + '.tl',
                 'bytes':100, 'record_sha256':digest * 64, 'duration_seconds':30,
                 'technical':{'source':'native-inspect', 'channels':2, 'route':0,
                              'allocated_voices':1, 'events':2}}
                for kind, identifier, digest in [('standard','parted','a'), ('keys','bell','b')]]}]}

    def test_merge_flat_outputs_replay_alone_and_preserve_sources(self):
        before = {path.name:path.read_bytes() for path in self.root.glob('*.tl')}
        manifest = self.export()
        self.assertEqual(manifest['songs'][0]['revision'], 'first')
        self.assertEqual(len(list((self.output / 'songs').iterdir())), 2)
        for variant in manifest['songs'][0]['variants']:
            target = self.output / variant['path']
            self.assertEqual(variant['bytes'], target.stat().st_size)
            self.assertEqual(variant['record_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(variant['id'], next(v['id'] for v in self.value['songs'][0]['variants'] if v['kind'] == variant['kind']))
            self.assertEqual(variant['technical']['source'], 'native-inspect')
            self.assertEqual(variant['technical']['channels'], 1)
            self.assertEqual(variant['technical']['route'], 0)
            self.assertNotIn('discovery', variant)
            self.assertGreater(variant['duration_seconds'], 0)
            result = subprocess.run([str(HARNESS), '--play', target.name], cwd=target.parent,
                                    check=True, capture_output=True, text=True)
            self.assertIn('match=yes block_indep=yes', result.stdout)
            play = dict(item.split('=', 1) for item in next(
                line for line in result.stdout.splitlines() if line.startswith('PLAY ')).split()[1:])
            self.assertEqual(variant['levels'], {
                'source': 'native-play-once', 'scope': 'final-pcm16-all-channels',
                'record_sha256': variant['record_sha256'], 'sample_rate_hz': int(play['fs']),
                'frames': int(play['samples']), 'channels': int(play.get('channels', '1')),
                'sample_peak_lsb': int(play['peak']), 'sample_peak_dbfs': float(play['peak_dbfs']),
                'rms_dbfs': float(play['rms_dbfs']), 'native_clipped': int(play['clipped'])})
        self.assertEqual((self.output / 'songs/suite-one-standard.tl').read_text(), self.sine)
        self.assertEqual(manifest['songs'][0]['variants'][0]['duration_seconds'], 2)
        self.assertEqual(before, {path.name:path.read_bytes() for path in self.root.glob('*.tl')})
        self.assertFalse(list(self.root.rglob('*.wav')))

    def test_native_level_mapping_preserves_silence_and_channel_context(self):
        variant = {'record_sha256': 'a' * 64, 'duration_seconds': 2, 'technical': {'channels': 2}}
        play = dict(fs='48000', samples='96000', channels='2', peak='0', peak_dbfs='-inf', rms_dbfs='-inf', clipped='0')
        levels = exporter.levels_from_play(play, variant)
        self.assertEqual((levels['sample_peak_lsb'], levels['sample_peak_dbfs'], levels['rms_dbfs']), (0, None, None))
        self.assertNotIn('Infinity', json.dumps(levels, allow_nan=False))
        self.assertEqual((levels['frames'], levels['channels'], levels['sample_rate_hz']), (96000, 2, 48000))
        # Field saturation is an aggregate counter, not bounded by output frames.
        play.update(peak='32768', peak_dbfs='0.00', rms_dbfs='-3.27', clipped='192001')
        measured = exporter.levels_from_play(play, variant)
        self.assertEqual((measured['sample_peak_dbfs'], measured['rms_dbfs'], measured['native_clipped']), (0, -3.27, 192001))
        del play['channels']
        with self.assertRaisesRegex(ValueError, 'sample context'):
            exporter.levels_from_play(play, variant)

    def test_refresh_preserves_measured_levels_and_accepts_unknown_legacy_levels(self):
        manifest = self.export()
        standard, keys = manifest['songs'][0]['variants']
        del standard['levels']  # Old exports do not acquire invented measurements.
        target = self.output / 'songs.json'
        target.write_text(json.dumps(manifest))
        with patch.object(subprocess, 'run', side_effect=AssertionError('Level metadata must not require rerendering.')):
            self.assertEqual(exporter.refresh_catalog(self.catalog, self.output, root=self.root), manifest)
            self.assertEqual(exporter.check_output(self.catalog, self.output, root=self.root), manifest)
        self.assertEqual(json.loads((self.output / 'catalog/index.json').read_bytes())['total_songs'], 0)
        self.assertEqual(json.loads(target.read_bytes())['songs'][0]['variants'][1]['levels'], keys['levels'])
        self.assertNotIn('levels', json.loads(target.read_bytes())['songs'][0]['variants'][0])
        self.assertEqual(target.read_text(), json.dumps(manifest), 'Refresh must leave stored measurements intact.')
        # A later ordinary export captures fresh measurements for each exact variant.
        self.value['songs'][0]['variants'][0]['paths'] = ['bell.tl']
        self.save()
        refreshed = self.export()['songs'][0]['variants'][0]
        self.assertNotEqual(refreshed['record_sha256'], standard['record_sha256'])
        self.assertEqual(refreshed['levels'], keys['levels'])

    def test_level_metadata_rejects_stale_identity_and_invalid_measurements(self):
        manifest = self.export()
        for changes in [{'record_sha256': '0' * 64}, {'channels': 2}, {'frames': 96001},
                        {'sample_rate_hz': True}, {'frames': 1 << 32}, {'native_clipped': -1},
                        {'native_clipped': True}, {'sample_peak_lsb': 32769}, {'sample_peak_lsb': 0},
                        {'sample_peak_dbfs': None}, {'sample_peak_dbfs': float('nan')},
                        {'rms_dbfs': float('-inf')}, {'rms_dbfs': 1}, {'scope': 'LUFS'}, {'lufs': -14}]:
            with self.subTest(changes=changes):
                changed = copy.deepcopy(manifest)
                changed['songs'][0]['variants'][0]['levels'].update(changes)
                (self.output / 'songs.json').write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    exporter.check_output(self.catalog, self.output, root=self.root, check_browse=False)
        changed = copy.deepcopy(manifest)
        changed['songs'][0]['variants'][0]['levels'] = changed['songs'][0]['variants'][1]['levels']
        (self.output / 'songs.json').write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'canonical identity'):
            exporter.refresh_catalog(self.catalog, self.output, root=self.root)

    def discovery(self):
        return {'record_sha256':hashlib.sha256(self.sine.encode()).hexdigest(), 'source':'header.tl',
                'metadata':{'genre':{'value':'test-study','source':'retained-score'},
                            'tempo':{'min_bpm':110,'max_bpm':174,'source':'retained-score'}},
                'featured':{'order':1,'note':'Editorial starting point'},
                'showcase':{'order':2,'note':'Authored changing-tempo example'}}

    def test_sourced_discovery_is_bound_to_one_exact_canonical_variant(self):
        discovery = self.discovery()
        self.value['songs'][0]['variants'][0]['discovery'] = discovery
        self.save()
        manifest = self.export()
        standard, keys = manifest['songs'][0]['variants']
        self.assertEqual(standard['discovery'], discovery)
        self.assertEqual(standard['record_sha256'], discovery['record_sha256'])
        self.assertEqual(standard['discovery']['metadata']['tempo']['min_bpm'], 110)
        self.assertEqual(standard['discovery']['metadata']['tempo']['max_bpm'], 174)
        self.assertNotIn('discovery', keys, 'Labels do not transfer by title or song family.')
        self.assertNotIn('provenance', standard['discovery'], 'Missing model remains unknown.')
        self.assertEqual((self.output / standard['path']).read_text(), self.sine)

        # Backfilling a reported model retains exact attribution without rendering
        # or lending that label to another saved profile, even when not browsable.
        discovery['provenance'] = {'model':'reported-model-v1', 'model_source':'client-reported'}
        self.save()
        with patch.object(subprocess, 'run', side_effect=AssertionError('Attribution refresh must not render.')):
            refreshed = exporter.refresh_metadata(self.catalog, self.output, root=self.root)
        standard, keys = refreshed['songs'][0]['variants']
        self.assertEqual(standard['discovery']['provenance'], discovery['provenance'])
        self.assertNotIn('discovery', keys)
        index = json.loads((self.output / 'catalog/index.json').read_bytes())
        self.assertEqual(index['total_songs'], 0)
        self.assertFalse(any(row['id'].startswith('model-') for row in index['categories']))
        self.assertEqual(exporter.catalog_summary(refreshed)['version_coverage']['model']['denominator'], 0)
        self.assertEqual((self.output / standard['path']).read_text(), self.sine)

    def test_stale_discovery_identity_preserves_previous_export(self):
        self.export()
        before = self.snapshot()
        self.value['songs'][0]['variants'][0]['discovery'] = {**self.discovery(), 'record_sha256':'0' * 64}
        self.save()
        with self.assertRaisesRegex(ValueError, 'Discovery identity does not match'):
            self.export()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.output.glob('.songs-export-*')))

    def test_discovery_refuses_unsourced_guessed_or_invalid_fields(self):
        baseline = self.discovery()
        candidates = []
        for changes in [{'source':'../escape.txt'}, {'source':'missing.txt'}, {'record_sha256':'short'},
                        {'metadata':{'tempo':{'source':'retained-score','min_bpm':150,'max_bpm':110}}},
                        {'metadata':{'tempo':{'source':'retained-score','min_bpm':True,'max_bpm':174}}},
                        {'metadata':{'tempo':{'source':'retained-score','min_bpm':110,'max_bpm':float('inf')}}},
                        {'metadata':{'tempo':{'source':'retained-score','min_bpm':110}}},
                        {'metadata':{'genre':{'value':'test-study','source':'filename'}}},
                        {'provenance':{'model':'guessed'}},
                        {'featured':{'order':False,'note':'Invalid'}},
                        {'showcase':{'order':1,'note':'Control\ncharacter'}}, {'quality_score':100}]:
            candidates.append({**baseline, **changes})
        for discovery in candidates:
            with self.subTest(discovery=discovery):
                self.value['songs'][0]['variants'][0]['discovery'] = discovery
                self.save()
                with self.assertRaises(ValueError): exporter.catalog_entries(self.catalog, self.root)

    def test_editorial_order_is_explicit_and_unambiguous(self):
        standard, keys = self.value['songs'][0]['variants']
        standard['discovery'] = self.discovery()
        keys['discovery'] = copy.deepcopy(self.discovery())
        self.save()
        with self.assertRaisesRegex(ValueError, 'Duplicate editorial order'):
            exporter.catalog_entries(self.catalog, self.root)

    def test_recovered_declarations_keep_unknowns_clocks_and_profile_scope(self):
        value, _ = exporter.catalog_entries(ROOT / 'music/library.json', ROOT)
        songs = [song for song in value['songs'] if song['id'].startswith('songbook/')]
        self.assertEqual(len(songs), 47)
        standards = {song['id']:next(v for v in song['variants'] if v['kind'] == 'standard') for song in songs}
        self.assertEqual(sum('genre' in v['discovery']['metadata'] for v in standards.values()), 46)
        self.assertEqual(sum('tempo' in v['discovery']['metadata'] for v in standards.values()), 46)
        self.assertEqual(sum(v['discovery']['metadata'].get('genre', {}).get('source') == 'creator-declared'
                             for v in standards.values()), 45)
        for song in songs:
            for variant in song['variants']:
                discovery = variant.get('discovery', {})
                self.assertNotIn('provenance', discovery, 'A composing model cannot be inferred from its coordinator.')
                if variant['kind'] != 'standard':
                    self.assertNotIn('metadata', discovery, 'Recovery does not describe derived profiles.')
                else:
                    self.assertEqual(len(variant['paths']), 1)
                    self.assertEqual(hashlib.sha256((ROOT / variant['paths'][0]).read_bytes()).hexdigest(),
                                     discovery['record_sha256'])
        metadata = lambda suffix:next(v['discovery']['metadata'] for key, v in standards.items() if key.endswith('/' + suffix))
        self.assertEqual(metadata('sidewalk-meridian')['tempo'],
                         {'min_bpm':80, 'max_bpm':80, 'source':'retained-score'})
        self.assertEqual(metadata('faultline-atlas')['tempo'],
                         {'min_bpm':90, 'max_bpm':132, 'source':'retained-score'})
        self.assertEqual(metadata('clockmakers-garden')['tempo'],
                         {'min_bpm':60, 'max_bpm':100, 'source':'retained-score'})
        self.assertNotIn('tempo', metadata('lattice-breathing'))
        self.assertNotIn('genre', metadata('paper-lanterns'))

    def test_metadata_refresh_preserves_canonical_bytes_measurements_and_unrelated_assets(self):
        before = self.export()
        files = {v['path']:(self.output / v['path']).read_bytes() for v in before['songs'][0]['variants']}
        for name in ('sw.js', 'engines.json'):
            (self.output / name).write_text('owner bytes\n')
        discovery = self.discovery()
        discovery['metadata']['genre']['source'] = 'creator-declared'
        discovery['metadata']['tempo']['source'] = 'creator-declared'
        self.value['songs'][0]['variants'][0]['discovery'] = discovery
        self.save()
        with patch.object(subprocess, 'run', side_effect=AssertionError('Metadata refresh must not render.')):
            refreshed = exporter.refresh_metadata(self.catalog, self.output, root=self.root)
            self.assertEqual(exporter.check_output(self.catalog, self.output, root=self.root), refreshed)
        self.assertEqual(refreshed['songs'][0]['variants'][0]['discovery'], discovery)
        for old, new in zip(before['songs'][0]['variants'], refreshed['songs'][0]['variants']):
            self.assertEqual(old, {key:value for key, value in new.items() if key != 'discovery'})
        self.assertEqual(files, {name:(self.output / name).read_bytes() for name in files})
        for name in ('sw.js', 'engines.json'):
            self.assertEqual((self.output / name).read_text(), 'owner bytes\n')
        self.value['songs'][0]['variants'][0].pop('discovery')
        self.save()
        self.assertEqual(exporter.refresh_metadata(self.catalog, self.output, root=self.root), before)

    def test_metadata_refresh_refuses_wrong_identity_structure_and_rolls_back(self):
        self.export()
        before = self.snapshot()
        baseline = copy.deepcopy(self.value)
        self.value['songs'][0]['variants'][0]['discovery'] = {**self.discovery(), 'record_sha256':'0' * 64}
        self.save()
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            exporter.refresh_metadata(self.catalog, self.output, root=self.root)
        self.assertEqual(self.snapshot(), before)
        self.value = copy.deepcopy(baseline)
        self.value['songs'][0]['title'] = 'New structural title'
        self.save()
        with self.assertRaisesRegex(ValueError, 'song metadata'):
            exporter.refresh_metadata(self.catalog, self.output, root=self.root)
        self.assertEqual(self.snapshot(), before)
        self.value = baseline
        self.value['songs'][0]['variants'][0]['discovery'] = self.discovery()
        self.save()
        rename = Path.rename
        def fail_catalog(source, destination):
            if source.name == 'catalog' and source.parent.name.startswith('.metadata-export-'):
                raise OSError('fixture metadata rename failure')
            return rename(source, destination)
        with patch.object(Path, 'rename', fail_catalog):
            with self.assertRaisesRegex(OSError, 'metadata rename failure'):
                exporter.refresh_metadata(self.catalog, self.output, root=self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.output.glob('.metadata-export-*')))

    def test_summary_counts_compositions_not_profiles_and_preserves_unknown_models(self):
        def variant(kind, duration, genre=None, tempo=None, source='retained-score'):
            metadata = {}
            if genre:
                metadata['genre'] = {'value':genre, 'source':source}
            if tempo:
                metadata['tempo'] = {'min_bpm':tempo[0], 'max_bpm':tempo[1], 'source':source}
            return {'kind':kind, 'duration_seconds':duration, 'discovery':{'metadata':metadata}}
        manifest = {'version':1, 'songs':[
            {'variants':[variant('standard', 30, 'pulse', (80,80)), variant('keys', 900, 'other', (160,160))]},
            {'variants':[variant('standard', 40, 'chamber', (60,100), 'creator-declared')]},
            {'variants':[variant('standard', 90)]}]}
        summary = exporter.catalog_summary(manifest)
        self.assertEqual((summary['compositions'], summary['canonical_versions'], summary['selection']), (3,4,'standard'))
        duration = summary['duration_seconds']
        self.assertEqual({key:duration[key] for key in ('total','mean','median','min','max')},
                         {'total':160, 'mean':160 / 3, 'median':40, 'min':30, 'max':90})
        self.assertEqual([row['count'] for row in duration['distribution']], [0,2,1,0,0,0])
        self.assertEqual(summary['version_coverage']['genre'], {'denominator':4,'known':3,'unknown':1})
        genre = summary['declarations']['genre']
        self.assertEqual((genre['denominator'],genre['known'],genre['unknown']), (3,2,1))
        self.assertEqual(genre['breakdown']['items'], [{'value':'chamber','count':1},{'value':'pulse','count':1}])
        tempo = summary['declarations']['tempo']
        self.assertEqual((tempo['fixed'],tempo['ranged'],tempo['min_bpm'],tempo['max_bpm']), (1,1,60,100))
        self.assertEqual(summary['declarations']['model']['unknown'], 3)
        self.assertEqual(summary['declarations']['model']['breakdown']['items'], [])
        self.assertEqual(summary['version_coverage']['model']['unknown'], 4)
        # Serialization order is not a new snapshot; changing the actual data is.
        reordered = json.loads(json.dumps(manifest, sort_keys=True))
        self.assertEqual(exporter.catalog_summary(reordered), summary)
        reordered['songs'][0]['variants'][1]['duration_seconds'] = 901
        changed = exporter.catalog_summary(reordered)
        self.assertNotEqual(changed['as_of'], summary['as_of'])
        self.assertEqual(changed['duration_seconds'], summary['duration_seconds'])

    def test_summary_bounds_breakdowns_with_explicit_omissions_and_duration_edges(self):
        songs = [{'variants':[{'kind':'standard', 'duration_seconds':duration,
                              'discovery':{'metadata':{'genre':{'value':f'genre-{number:03}', 'source':'creator-declared'}}}}]}
                 for number, duration in enumerate([30,60,120,180,300] + [21] * 35)]
        summary = exporter.catalog_summary({'version':1,'songs':songs})
        genre = summary['declarations']['genre']['breakdown']
        self.assertEqual((genre['total_labels'],len(genre['items']),genre['omitted_labels'],genre['omitted_count']), (40,32,8,8))
        self.assertEqual(genre['items'][0], {'value':'genre-000','count':1})
        self.assertEqual([row['count'] for row in summary['duration_seconds']['distribution']], [35,1,1,1,1,1])
        self.assertEqual(summary['declarations']['tempo']['unknown'], 40)
        self.assertIsNone(summary['declarations']['tempo']['min_bpm'])
        self.assertEqual(summary['duration_seconds']['median'], 21)
        self.assertLess(len(json.dumps(summary).encode()), 16384)

    def test_browser_duration_projection_filters_all_categories_and_summary_without_changing_archive(self):
        manifest = {'version':1, 'songs':[]}
        cases = [('short',19,900), ('boundary',20,30), ('just-over',20 + 1 / 48000,25),
                 ('legacy-long',301,301), ('short-profile',40,20)]
        for number, (identifier, original_duration, keys_duration) in enumerate(cases):
            song = self.browser_manifest()['songs'][0]
            song.update(id=identifier, title=identifier, collection=identifier)
            for offset, (variant, duration) in enumerate(zip(song['variants'], [original_duration, keys_duration])):
                variant.update(id=identifier + '-' + variant['kind'], duration_seconds=duration)
                label = variant['id']
                variant['discovery'] = {
                    'metadata':{'genre':{'value':label, 'source':'creator-declared'}},
                    'provenance':{'model':label, 'model_source':'client-reported'},
                    'featured':{'order':2 * number + offset, 'note':'Selected profile'},
                    'showcase':{'order':2 * number + offset, 'note':'Selected profile'}}
            manifest['songs'].append(song)
        archive = copy.deepcopy(manifest)
        documents = {name:json.loads(data) for name, data in exporter.catalog_documents(manifest).items()}
        index, summary = documents['catalog/index.json'], documents['catalog/summary.json']
        self.assertEqual(index['total_songs'], 3)
        self.assertEqual((summary['compositions'], summary['canonical_versions']), (3,5))
        self.assertEqual(summary['duration_seconds']['denominator'], index['total_songs'])
        self.assertAlmostEqual(summary['duration_seconds']['total'], 361 + 1 / 48000)
        self.assertEqual(summary['duration_seconds']['median'], 40)
        self.assertEqual(summary['declarations']['model']['known'], 3)
        self.assertEqual(summary['version_coverage']['model']['known'], 5)
        full_identity = hashlib.sha256(json.dumps(archive, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(summary['as_of'], {'source':'songs.json', 'sha256':full_identity})
        general = documents['catalog/all-1.json']['items']
        self.assertEqual([row['id'] for row in general],
                         ['just-over-keys', 'legacy-long-keys', 'short-profile-standard'])
        expected = {'just-over-standard', 'just-over-keys', 'legacy-long-standard',
                    'legacy-long-keys', 'short-profile-standard'}
        for name, page in documents.items():
            if 'items' not in page:
                continue
            for row in page['items']:
                self.assertIn(row['id'], expected, name)
                self.assertGreater(row['duration_seconds'], 20)
                self.assertTrue(all(v['id'] in expected and v['duration_seconds'] > 20 for v in row['variants']))
        categories = index['categories']
        self.assertEqual({row['title'] for row in categories if row['id'].startswith('collection-')},
                         {'just-over', 'legacy-long', 'short-profile'})
        for field, prefix in [('genre','Genre: '), ('model','Model: ')]:
            self.assertEqual({row['title'] for row in categories if row['id'].startswith(field + '-')},
                             {prefix + label for label in expected})
        for category in ('featured','showcase'):
            self.assertEqual({row['id'] for row in documents['catalog/' + category + '-1.json']['items']}, expected)
        self.assertEqual(manifest, archive, 'Browser projection must not mutate complete archive metadata.')

    def test_browser_handles_empty_collection_and_refuses_invalid_archived_durations(self):
        documents = {name:json.loads(data) for name, data in exporter.catalog_documents({'version':1,'songs':[]}).items()}
        self.assertEqual(set(documents), {'catalog/index.json','catalog/summary.json'})
        index, summary = documents['catalog/index.json'], documents['catalog/summary.json']
        self.assertEqual(index['total_songs'], 0)
        self.assertTrue(all(row['count'] == 0 and row['first_page'] is None for row in index['categories']))
        self.assertEqual((summary['compositions'],summary['canonical_versions']), (0,0))
        self.assertEqual(summary['duration_seconds']['total'], 0)
        self.assertIsNone(summary['duration_seconds']['mean'])
        self.assertIsNone(summary['duration_seconds']['median'])
        for value in (0, -1, True, float('nan'), float('inf'), '21'):
            with self.subTest(duration=value):
                manifest = self.browser_manifest()
                manifest['songs'][0]['variants'][0]['duration_seconds'] = value
                with self.assertRaisesRegex(ValueError, 'positive native durations'):
                    exporter.catalog_documents(manifest)

    def test_current_source_catalog_keeps_legacy_records_and_eight_favorites(self):
        value, _ = exporter.catalog_entries(ROOT / 'music/library.json', ROOT)
        selections = [(song['id'], variant) for song in value['songs'] for variant in song['variants']
                      if 'featured' in variant.get('discovery', {})]
        self.assertEqual(len(selections), 8)
        featured = sorted(selections, key=lambda row:row[1]['discovery']['featured']['order'])
        self.assertEqual(featured[0][0], 'music/foundry')
        self.assertTrue(any('discovery' not in variant for song in value['songs'] for variant in song['variants']))
        by_song = dict(selections)
        self.assertNotIn('preview-lantern', by_song)
        self.assertTrue(any(song['id'] == 'preview-lantern' for song in value['songs']))
        for song_id in ('music/quench', 'music/rivet-line', 'music/wind-rose'):
            self.assertEqual(by_song[song_id]['kind'], 'keys')
        self.assertEqual(by_song['music/dubplate']['kind'], 'standard')
        self.assertEqual(by_song['songbook/recursive-exploration/root-and-branches']['kind'], 'standard')
        self.assertEqual(by_song['music/hurry-up']['discovery']['metadata']['tempo'],
                         {'min_bpm':110,'max_bpm':174,'source':'retained-score'})

        # Recovered labels belong to exact profiles independently of curation;
        # an unknown composing model is not attributed to the coordinator.
        recovered = {'music/rivet-line': ('warehouse-techno', 125),
                     'music/quench': ('industrial-broken-beat', 160),
                     'music/night-press': ('industrial-dub', 120)}
        labelled = [song for song in value['songs'] if song['id'] in recovered]
        self.assertEqual(len(labelled), len(recovered))
        hashes = set()
        for song in labelled:
            genre, bpm = recovered[song['id']]
            self.assertEqual({variant['kind'] for variant in song['variants']}, {'standard', 'keys', 'mirror'})
            for variant in song['variants']:
                discovery = variant['discovery']
                expected_fields = {'record_sha256', 'source', 'metadata'}
                if song['id'] in by_song and variant is by_song[song['id']]:
                    expected_fields.add('featured')
                self.assertEqual(set(discovery), expected_fields)
                self.assertEqual(discovery['source'], song['id'] + '/session.tl')
                self.assertEqual(discovery['metadata'], {
                    'genre': {'value': genre, 'source': 'retained-score'},
                    'tempo': {'min_bpm': bpm, 'max_bpm': bpm, 'source': 'retained-score'}})
                hashes.add(discovery['record_sha256'])
        self.assertEqual(len(hashes), 9, 'Each saved profile retains its own canonical identity.')

    def test_hash_failure_or_missing_hash_preserves_previous_export(self):
        self.export()
        before = self.snapshot()
        original = (self.root / 'header.tl').read_text()
        for text in [original.replace('hash e76dc5bad8783650', 'hash 0000000000000000'),
                     original.replace('hash e76dc5bad8783650\n', '')]:
            (self.root / 'header.tl').write_text(text)
            with self.assertRaises(ValueError):
                self.export()
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(list(self.output.glob('.songs-export-*')))

    def test_invalid_catalog_paths_and_filename_collisions_refuse_before_publication(self):
        self.export()
        before = self.snapshot()
        baseline = copy.deepcopy(self.value)
        candidates = []
        duplicate = copy.deepcopy(baseline); duplicate['songs'].append(copy.deepcopy(duplicate['songs'][0])); candidates.append(duplicate)
        collision = copy.deepcopy(baseline); other = copy.deepcopy(collision['songs'][0]); other['id']='suite-one'
        for variant in other['variants']: variant['id'] += '-other'
        collision['songs'].append(other); candidates.append(collision)
        for paths in [['../escape.tl'], ['/absolute.tl'], ['missing.tl'], ['./header.tl'], ['header.tl','./header.tl']]:
            value = copy.deepcopy(baseline); value['songs'][0]['variants'][0]['paths']=paths; candidates.append(value)
        missing_standard = copy.deepcopy(baseline); missing_standard['songs'][0]['variants'].pop(0); candidates.append(missing_standard)
        for value in candidates:
            self.value = value; self.save()
            with self.assertRaises(ValueError): self.export()
            self.assertEqual(self.snapshot(), before)
        self.catalog.write_text('{"version":1,"version":1,"songs":[]}')
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'): self.export()

    def test_final_rename_error_rolls_back_folder_and_manifest(self):
        self.export()
        before = self.snapshot()
        self.value['songs'][0]['title'] = 'Replacement'; self.save()
        original = Path.rename
        def rename(source, destination):
            if source.name == 'songs.json' and source.parent.name.startswith('.songs-export-'):
                raise OSError('fixture rename failure')
            return original(source, destination)
        with patch.object(Path, 'rename', rename):
            with self.assertRaisesRegex(OSError, 'fixture rename failure'): self.export()
        self.assertEqual(self.snapshot(), before)

    def make_fixture(self):
        (self.root / 'tools').mkdir()
        shutil.copyfile(ROOT / 'tools/export_library.py', self.root / 'tools/export_library.py')
        (self.root / 'music').mkdir()
        shutil.copyfile(self.catalog, self.root / 'music/library.json')
        source = (ROOT / 'Makefile').read_text()
        rules = source[source.index('SONG_JOBS ?='):source.index('.PHONY: songs') + len('.PHONY: songs')]
        (self.root / 'Makefile').write_text('B := ' + str(HARNESS.parent) + '\n' + rules + '\n')

    def test_make_timestamps_skip_unchanged_export_and_notice_source_changes(self):
        self.make_fixture()
        self.export()
        self.assertEqual(subprocess.run(['make','-q','songs'], cwd=self.root).returncode, 0)
        changed = (self.output / 'songs.json').stat().st_mtime + 1
        os.utime(self.root / 'event parts.tl', (changed, changed))
        self.assertEqual(subprocess.run(['make','-q','songs'], cwd=self.root).returncode, 1)
        (self.root / 'event parts.tl').unlink()
        refused = subprocess.run(['make','-q','songs'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('Cannot read song catalog inputs', refused.stderr)

    def test_list_inputs_includes_discovery_support_and_make_notices_its_changes(self):
        (self.root / 'score notes.txt').write_text('Retained authored tempo clock.\n')
        self.value['songs'][0]['variants'][0]['discovery'] = {**self.discovery(), 'source':'score notes.txt'}
        self.save()
        self.make_fixture()
        self.export()
        listed = subprocess.run(['python3', 'tools/export_library.py', '--list-inputs'], cwd=self.root,
                                check=True, capture_output=True, text=True).stdout
        for name in ['header.tl', 'event parts.tl', 'bell.tl', 'score notes.txt']:
            self.assertIn(str((self.root / name).resolve()).replace(' ', '\\ '), listed.splitlines())
        self.assertEqual(subprocess.run(['make', '-q', 'songs'], cwd=self.root).returncode, 0)
        changed = (self.output / 'songs.json').stat().st_mtime + 1
        os.utime(self.root / 'score notes.txt', (changed, changed))
        self.assertEqual(subprocess.run(['make', '-q', 'songs'], cwd=self.root).returncode, 1)
        (self.root / 'score notes.txt').unlink()
        refused = subprocess.run(['make', '-q', 'songs'], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('Cannot read song catalog inputs', refused.stderr)

    def test_complete_output_check_never_invokes_native_renderer(self):
        manifest = self.export()
        with patch.object(subprocess, 'run', side_effect=AssertionError('Output check must not render.')):
            self.assertEqual(exporter.check_output(self.catalog, self.output, root=self.root), manifest)
        checked = subprocess.run(['python3', str(ROOT / 'tools/export_library.py'), '--catalog', str(self.catalog),
                                  '--root', str(self.root), '--output', str(self.output), '--check-output',
                                  '--harness', str(self.root / 'absent-harness')], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_browsing_keeps_curated_identity_and_uses_keys_for_general_lists(self):
        manifest = self.browser_manifest()
        discovery = {**self.discovery(), 'record_sha256':manifest['songs'][0]['variants'][0]['record_sha256']}
        discovery['provenance'] = {'model':'reported-model-v1', 'model_source':'client-reported'}
        manifest['songs'][0]['variants'][0]['discovery'] = discovery
        documents = exporter.catalog_documents(manifest)
        index = json.loads(documents['catalog/index.json'])
        self.assertEqual(index['page_size'], 24)
        self.assertEqual(index['total_songs'], 1)
        self.assertEqual(index['summary'], 'catalog/summary.json')
        self.assertEqual(json.loads(documents[index['summary']]), exporter.catalog_summary(manifest))
        featured = json.loads(documents[index['categories'][0]['first_page']])
        self.assertEqual(featured['items'][0]['variant'], 'standard')
        self.assertEqual(featured['items'][0]['record_sha256'], manifest['songs'][0]['variants'][0]['record_sha256'])
        general = json.loads(documents['catalog/all-1.json'])
        self.assertEqual(general['items'][0]['variant'], 'keys')
        self.assertNotIn('metadata', general['items'][0])
        self.assertNotIn('provenance', general['items'][0])
        self.assertEqual(general['items'][0]['variants'], manifest['songs'][0]['variants'])
        self.assertEqual(featured['items'][0]['variants'], general['items'][0]['variants'])
        self.assertEqual(general['items'][0]['variants'][0]['discovery'], discovery)
        model = next(row for row in index['categories'] if row['title'] == 'Model: reported-model-v1')
        self.assertEqual(model['count'], 1)
        row = json.loads(documents[model['first_page']])['items'][0]
        self.assertEqual((row['id'], row['variant']), ('parted', 'standard'))
        self.assertEqual(row['provenance'], discovery['provenance'])
        self.assertEqual(exporter.catalog_summary(manifest)['version_coverage']['model']['unknown'], 1)
        self.assertIsNone(general['next'])

    def test_short_native_exports_remain_complete_with_empty_refreshable_browser(self):
        manifest = self.export()
        originals = {v['path']:(self.output / v['path']).read_bytes() for v in manifest['songs'][0]['variants']}
        manifest_bytes = (self.output / 'songs.json').read_bytes()
        index = json.loads((self.output / 'catalog/index.json').read_bytes())
        self.assertEqual(index['total_songs'], 0)
        self.assertTrue(all(row['count'] == 0 and row['first_page'] is None for row in index['categories']))
        self.assertEqual(set(path.name for path in (self.output / 'catalog').iterdir()), {'index.json','summary.json'})
        (self.output / 'catalog/index.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'browsing page differs'):
            exporter.check_output(self.catalog, self.output, root=self.root)
        with patch.object(subprocess, 'run', side_effect=AssertionError('Index refresh must not render.')):
            exporter.refresh_catalog(self.catalog, self.output, root=self.root)
        self.assertEqual(exporter.check_output(self.catalog, self.output, root=self.root), manifest)
        self.assertEqual((self.output / 'songs.json').read_bytes(), manifest_bytes)
        self.assertEqual(originals, {name:(self.output / name).read_bytes() for name in originals})

    def test_ten_thousand_song_catalog_uses_bounded_pages_without_audio_or_tl_copies(self):
        base = self.browser_manifest()['songs'][0]
        songs = [{**base, 'id': 'song-' + str(i), 'title': 'Song ' + str(i),
                  'variants': [{**v, 'id': 'song-' + str(i) + '-' + v['kind']} for v in base['variants']]}
                 for i in range(10000)]
        documents = exporter.catalog_documents({'version': 1, 'songs': songs})
        index = json.loads(documents['catalog/index.json'])
        self.assertEqual(index['total_songs'], 10000)
        self.assertLess(len(documents['catalog/index.json']), 4096)
        seen, next_page = [], 'catalog/all-1.json'
        while next_page:
            page = json.loads(documents[next_page])
            self.assertLessEqual(len(page['items']), 24)
            self.assertEqual(page['total'], 10000)
            seen.extend(row['id'] for row in page['items'])
            next_page = page['next']
        self.assertEqual(seen, ['song-' + str(i) + '-keys' for i in sorted(range(10000), key=str)])
        self.assertTrue(all(name.endswith('.json') and len(data) <= 262144 for name, data in documents.items()))

    def test_alphabetical_catalog_orders_every_category_before_pagination_but_keeps_editorial_rank(self):
        songs = []
        for number in reversed(range(35)):
            song = self.browser_manifest()['songs'][0]
            song.update(id=f'song-{number:02}', title=f'Song {number:02}')
            for variant in song['variants']:
                variant['id'] = song['id'] + '-' + variant['kind']
            original = song['variants'][0]
            original['discovery'] = {
                'metadata':{'genre':{'value':'study', 'source':'creator-declared'}},
                'provenance':{'model':'reported-model', 'model_source':'client-reported'}}
            if number in (34, 0):
                original['discovery']['featured'] = {'order':1 if number == 34 else 2, 'note':'Editorial order'}
            songs.append(song)
        manifest = {'version':1, 'songs':songs}
        archive = copy.deepcopy(manifest)
        original_summary = exporter.catalog_summary(manifest)
        documents = exporter.catalog_documents(manifest)
        index = json.loads(documents['catalog/index.json'])
        for category in index['categories']:
            if category['id'] in ('featured','showcase'):
                continue
            rows, path, lengths = [], category['first_page'], []
            while path:
                page = json.loads(documents[path])
                rows.extend(page['items']); lengths.append(len(page['items'])); path = page['next']
            self.assertEqual(lengths, [24,11], category['id'])
            self.assertEqual([row['title'] for row in rows], [f'Song {number:02}' for number in range(35)], category['id'])
            expected_kind = 'keys' if category['id'] == 'all' or category['id'].startswith('collection-') else 'standard'
            self.assertTrue(all(row['variant'] == expected_kind for row in rows), category['id'])
            if expected_kind == 'keys':
                self.assertTrue(all('provenance' not in row for row in rows), 'Model attribution stays on Originals.')
        featured = json.loads(documents['catalog/featured-1.json'])['items']
        self.assertEqual([row['title'] for row in featured], ['Song 34','Song 00'])
        reordered = exporter.catalog_documents({'version':1, 'songs':list(reversed(songs))})
        for name, data in documents.items():
            if name != 'catalog/summary.json':
                self.assertEqual(reordered[name], data, name)
        self.assertEqual(json.loads(documents['catalog/summary.json']), original_summary)
        self.assertEqual(manifest, archive, 'Sorting must not modify the full manifest, profiles or metadata.')

    def test_alphabetical_catalog_normalizes_case_unicode_and_collection_ties(self):
        cases = [('alpha-c','ＡＬＰＨＡ','Gamma'), ('accent-b','écho','Same'),
                 ('strasse-b','STRASSE','Same'), ('alpha-b','ALPHA','Ｂeta'),
                 ('accent-a','e\u0301cho','same'), ('strasse-a','Straße','same'),
                 ('beta','bEta','First'), ('alpha-a','alpha','beta')]
        songs = []
        for identifier, title, collection in cases:
            song = self.browser_manifest()['songs'][0]
            song.update(id=identifier, title=title, collection=collection)
            for variant in song['variants']:
                variant['id'] = identifier + '-' + variant['kind']
            songs.append(song)
        expected = ['alpha-a','alpha-b','alpha-c','beta','strasse-a','strasse-b','accent-a','accent-b']
        for rows in (songs, list(reversed(songs))):
            documents = exporter.catalog_documents({'version':1, 'songs':rows})
            page = json.loads(documents['catalog/all-1.json'])
            self.assertEqual([row['id'] for row in page['items']], [identifier + '-keys' for identifier in expected])
            # Normalization is only a sort key: visible titles retain authored spelling.
            self.assertEqual({row['id']:row['title'] for row in page['items']},
                             {identifier + '-keys':title for identifier,title,_ in cases})

    def test_editorial_profiles_of_one_song_keep_distinct_row_identities(self):
        manifest = self.browser_manifest()
        for order, variant in enumerate(manifest['songs'][0]['variants'], 1):
            variant['discovery'] = {'featured': {'order': order, 'note': 'Selected profile'}}
        rows = json.loads(exporter.catalog_documents(manifest)['catalog/featured-1.json'])['items']
        self.assertEqual([row['id'] for row in rows], ['parted', 'bell'])
        self.assertEqual([row['variant'] for row in rows], ['standard', 'keys'])
        self.assertEqual(len({row['record_sha256'] for row in rows}), 2)

    def test_catalog_replacement_cannot_delete_its_inputs(self):
        self.export()
        baseline = copy.deepcopy(self.value)
        for kind in ('timeline', 'catalog', 'discovery'):
            with self.subTest(kind=kind):
                self.value = copy.deepcopy(baseline)
                catalog = self.catalog
                nested = self.output / 'catalog' / ('input.tl' if kind == 'timeline' else 'input.json')
                if kind == 'timeline':
                    shutil.copyfile(self.root / 'bell.tl', nested)
                    self.value['songs'][0]['variants'][1]['paths'] = [nested.relative_to(self.root).as_posix()]
                elif kind == 'discovery':
                    nested.write_text('Retained editorial source')
                    self.value['songs'][0]['variants'][0]['discovery'] = {
                        **self.discovery(), 'source': nested.relative_to(self.root).as_posix()}
                self.save()
                if kind == 'catalog':
                    shutil.copyfile(self.catalog, nested)
                    catalog = nested
                before = {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob('*') if p.is_file()}
                with patch.object(subprocess, 'run', side_effect=AssertionError('Refuse before rendering.')):
                    for action in (lambda: exporter.export_library(catalog, self.output, HARNESS, root=self.root),
                                   lambda: exporter.refresh_catalog(catalog, self.output, root=self.root)):
                        with self.assertRaisesRegex(ValueError, 'replace its own sources'):
                            action()
                self.assertEqual(before, {p.relative_to(self.output): p.read_bytes() for p in self.output.rglob('*') if p.is_file()})
                nested.unlink()

    def test_output_check_refuses_stale_metadata_and_corrupt_canonical_files(self):
        manifest = self.export()
        target = self.output / 'songs.json'
        variants = manifest['songs'][0]['variants']
        for mutate in [lambda value:value['songs'][0].update(title='Stale title'),
                       lambda value:value['songs'][0]['variants'][0].update(id='wrong-source-identity'),
                       lambda value:value['songs'][0]['variants'][0].update(discovery=self.discovery()),
                       lambda value:value['songs'][0]['variants'][0]['technical'].update(channels=16)]:
            changed = copy.deepcopy(manifest)
            mutate(changed)
            target.write_text(json.dumps(changed))
            with self.assertRaises(ValueError): exporter.check_output(self.catalog, self.output, root=self.root)
        target.write_text(json.dumps(manifest))
        (self.output / variants[0]['path']).write_text('corrupt canonical TL\n')
        with self.assertRaisesRegex(ValueError, 'size or SHA-256'):
            exporter.check_output(self.catalog, self.output, root=self.root)

    def test_make_repairs_newer_partial_manifest_and_missing_canonical_file(self):
        self.make_fixture()
        manifest = self.export()
        target = self.output / 'songs.json'
        partial = copy.deepcopy(manifest)
        partial['songs'][0]['variants'].pop()
        target.write_text(json.dumps(partial))
        newer = max(path.stat().st_mtime for path in self.root.rglob('*') if path.is_file()) + 10
        os.utime(target, (newer, newer))
        for damage in ['subset', 'missing']:
            if damage == 'missing':
                (self.output / manifest['songs'][0]['variants'][1]['path']).unlink()
            self.assertEqual(subprocess.run(['make', '-q', 'songs'], cwd=self.root).returncode, 1)
            repaired = subprocess.run(['make', '-s', 'songs'], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(exporter.check_output(self.catalog, self.output, root=self.root), manifest)
            self.assertEqual(subprocess.run(['make', '-q', 'songs'], cwd=self.root).returncode, 0)


if __name__ == '__main__':
    unittest.main()
