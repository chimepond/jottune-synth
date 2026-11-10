#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Export one native-validated canonical TL per song variant, without audio files.

The native --play-once checks saved PCM hashes; block independence is unchecked.
All records are staged before replacing a previous export.
Optional levels describe that exact canonical variant, not perceived loudness.
Legacy exports without levels remain valid; catalog refresh never renders.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import unicodedata

_spec = importlib.util.spec_from_file_location("export_tl_document", Path(__file__).with_name("tl_document.py"))
document = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(document)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PAGE_SIZE = 24
CATALOG_JSON_LIMIT = 262144
SUMMARY_BREAKDOWN_LIMIT = 32
DECLARATION_SOURCES = ('retained-score', 'creator-declared')
MADE_BY = ('agent', 'human', 'mixed')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON member: ' + key)
        result[key] = value
    return result


def validate_discovery(value, root):
    """Validate optional source labels; native export checks their exact identity.

    Recovered creator declarations remain distinct from retained score facts.
    Anonymous service declarations cannot claim these editorial sources.
    Missing labels stay absent; a source label is not an independent assessment.
    """
    require(isinstance(value, dict) and {'record_sha256', 'source'} <= set(value) and
            set(value) <= {'record_sha256', 'document_sha256', 'source', 'metadata', 'provenance', 'featured', 'showcase'},
            'Invalid discovery fields.')
    require(isinstance(value['record_sha256'], str) and re.fullmatch('[a-f0-9]{64}', value['record_sha256']),
            'Discovery requires an exact canonical record SHA-256.')
    if 'document_sha256' in value:
        require(isinstance(value['document_sha256'], str) and re.fullmatch('[a-f0-9]{64}', value['document_sha256']),
                'Discovery full-file checksum is invalid.')
    source = value['source']
    require(isinstance(source, str) and source and not PurePosixPath(source).is_absolute() and
            all(part not in ('', '.', '..') for part in source.split('/')) and '\\' not in source and
            not any(ord(c) < 32 for c in source), 'Discovery source must be a repository-relative file.')
    path = (root / source).resolve()
    require(path.is_relative_to(root) and path.is_file(), 'Missing discovery source or path outside source root.')
    if 'metadata' in value:
        metadata = value['metadata']
        require(isinstance(metadata, dict) and set(metadata) <= {'genre', 'tempo'}, 'Invalid discovery metadata.')
        if 'genre' in metadata:
            genre = metadata['genre']
            require(isinstance(genre, dict) and set(genre) == {'value', 'source'} and
                    isinstance(genre['value'], str) and re.fullmatch('[A-Za-z0-9._:+/@-]{1,160}', genre['value']) and
                    genre['source'] in DECLARATION_SOURCES, 'Genre needs an explicit supported declaration source.')
        if 'tempo' in metadata:
            tempo = metadata['tempo']
            require(isinstance(tempo, dict) and set(tempo) == {'min_bpm', 'max_bpm', 'source'} and
                    tempo['source'] in DECLARATION_SOURCES and
                    all(type(tempo[key]) in (int, float) and math.isfinite(tempo[key]) for key in ('min_bpm', 'max_bpm')) and
                    0 < tempo['min_bpm'] <= tempo['max_bpm'] <= 9007199254740991,
                    'Tempo needs an explicit supported source and positive BPM range.')
    if 'provenance' in value:
        provenance = value['provenance']
        # Songs contributed through tunes declare made_by, with the model pair when one was declared.
        # The archived songs carry only the model pair. The rule matches tunes/tools/validate.py.
        require(isinstance(provenance, dict) and provenance and set(provenance) <= {'made_by', 'model', 'model_source'} and
                ('model' in provenance) == ('model_source' in provenance) and
                provenance.get('made_by', 'agent') in MADE_BY, 'Invalid discovery model provenance.')
        require('model' not in provenance or
                isinstance(provenance['model'], str) and re.fullmatch('[A-Za-z0-9._:+/@-]{1,160}', provenance['model']) and
                provenance['model_source'] in ('client-reported', 'user-stated', 'unknown'), 'Invalid discovery model provenance.')
    for key in ('featured', 'showcase'):
        if key in value:
            editorial = value[key]
            require(isinstance(editorial, dict) and set(editorial) == {'order', 'note'} and
                    type(editorial['order']) is int and 0 < editorial['order'] <= 9007199254740991 and
                    isinstance(editorial['note'], str) and editorial['note'].strip() and
                    not any(ord(c) < 32 for c in editorial['note']), 'Invalid editorial selection.')


def resolve_discovery(song, variant, data, catalog_source):
    """Keep catalog authority while filling absent declarations from a portable TL."""
    parsed = document.parse_document(data)
    existing = variant.get('discovery')
    if not parsed['header']:
        require(existing is None or 'document_sha256' not in existing or
                existing['document_sha256'] == hashlib.sha256(data).hexdigest(),
                'Discovery full-file checksum differs from TL.')
        return existing
    metadata = parsed['metadata']
    result = json.loads(json.dumps(existing)) if existing is not None else {
        'record_sha256':document.body_sha256(data), 'source':catalog_source}
    external = {key:song[key] for key in ('title', 'collection')}
    labels, provenance = result.get('metadata', {}), result.get('provenance', {})
    if 'genre' in labels:
        external['genre'] = labels['genre']['value']
    if 'tempo' in labels:
        external['tempo'] = {key:labels['tempo'][key] for key in ('min_bpm', 'max_bpm')}
    if 'made_by' in provenance:
        external['made_by'] = provenance['made_by']
    if 'model' in provenance:
        external['model'] = {'id':provenance['model'], 'source':provenance['model_source']}
    for field in metadata.keys() & external.keys():
        require(metadata[field] == external[field], 'Embedded metadata conflicts with external ' + field + '.')
    checksum = hashlib.sha256(data).hexdigest()
    require('document_sha256' not in result or result['document_sha256'] == checksum,
            'Discovery full-file checksum differs from portable TL.')
    result['document_sha256'] = checksum
    labels = result.setdefault('metadata', {})
    if metadata.get('genre') is not None:
        labels.setdefault('genre', {'value':metadata['genre'], 'source':'creator-declared'})
    if metadata.get('tempo') is not None:
        labels.setdefault('tempo', {**metadata['tempo'], 'source':'creator-declared'})
    if not labels:
        result.pop('metadata', None)
    provenance = result.setdefault('provenance', {})
    if 'made_by' in metadata:
        provenance.setdefault('made_by', metadata['made_by'])
    if metadata.get('model') is not None:
        provenance.setdefault('model', metadata['model']['id'])
        provenance.setdefault('model_source', metadata['model']['source'])
    if not provenance:
        result.pop('provenance', None)
    return result


def catalog_entries(catalog, root):
    """Validate catalog structure and file membership, never TL statements."""
    root = Path(root).resolve()
    value = json.loads(Path(catalog).read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    require(isinstance(value, dict) and set(value) == {'version', 'songs'} and
            type(value['version']) is int and value['version'] == 1 and
            isinstance(value['songs'], list) and value['songs'], 'Expected a nonempty version1 song catalog.')
    songs, variants, names, tasks = set(), set(), set(), []
    editorial_orders = {'featured': set(), 'showcase': set()}
    text = lambda item: isinstance(item, str) and bool(item.strip())
    for song in value['songs']:
        require(isinstance(song, dict) and {'id','title','collection','variants'} <= set(song) and
                set(song) <= {'id','title','collection','revision','variants'}, 'Invalid song fields.')
        require(all(text(song[key]) for key in ('id','title','collection')) and
                ('revision' not in song or text(song['revision'])), 'Song metadata must be nonempty text.')
        require(song['id'] not in songs, 'Duplicate song ID: ' + song['id'])
        songs.add(song['id'])
        require(isinstance(song['variants'], list) and song['variants'], 'Song needs at least one variant.')
        kinds = set()
        slug = re.sub('[^a-z0-9]+', '-', song['id'].lower()).strip('-')
        require(slug, 'Song ID cannot form a safe filename: ' + song['id'])
        for variant in song['variants']:
            require(isinstance(variant, dict) and {'kind','id','paths'} <= set(variant) and
                    set(variant) <= {'kind','id','paths','discovery'}, 'Invalid variant fields.')
            kind = variant['kind']
            require(kind in ('standard', 'keys', 'mirror'), 'Invalid variant kind.')
            require(kind not in kinds and text(variant['id']) and variant['id'] not in variants,
                    'Duplicate variant kind or empty/duplicate variant ID.')
            kinds.add(kind)
            variants.add(variant['id'])
            if 'discovery' in variant:
                validate_discovery(variant['discovery'], root)
                for section in editorial_orders:
                    if section in variant['discovery']:
                        order = variant['discovery'][section]['order']
                        require(order not in editorial_orders[section], 'Duplicate editorial order: ' + section)
                        editorial_orders[section].add(order)
            filename = slug + '-' + kind + '.tl'
            require(len(filename) <= 240 and filename.casefold() not in names, 'Output filename collision or excessive length: ' + filename)
            names.add(filename.casefold())
            sources = variant['paths']
            require(isinstance(sources, list) and 1 <= len(sources) <= 17 and
                    all(isinstance(name, str) for name in sources) and len(set(sources)) == len(sources),
                    'Variant needs 1 to 17 distinct ordered source paths.')
            paths = []
            for name in sources:
                relative = PurePosixPath(name)
                require(name and not relative.is_absolute() and all(part not in ('', '.', '..') for part in name.split('/')) and
                        '\\' not in name and not any(ord(c) < 32 for c in name) and relative.suffix == '.tl',
                        'Source must be a relative TL path: ' + name)
                source = (root / name).resolve()
                require(source.is_relative_to(root) and source.is_file(), 'Missing source or path outside source root: ' + name)
                paths.append(source)
            require(len(set(paths)) == len(paths), 'Variant source paths resolve to the same file.')
            tasks.append((song['id'], kind, filename, paths))
        require('standard' in kinds, 'Song requires a Standard variant.')
    return value, tasks


def validate_levels(levels, variant):
    """Check bounded PLAY measurements without reading TL or rerendering.

    Peak/RMS pool all final interleaved PCM16 samples (full scale = 32768).
    Null dB values mean measured digital silence, not missing measurements.
    native_clipped is the renderer's aggregate saturation counter, which can
    include field and final saturation; it is not a count of output rail samples.
    These are neither true peak nor LUFS nor a musical-quality score.
    """
    fields = {'source', 'scope', 'record_sha256', 'sample_rate_hz', 'frames', 'channels',
              'sample_peak_lsb', 'sample_peak_dbfs', 'rms_dbfs', 'native_clipped'}
    require(isinstance(levels, dict) and set(levels) == fields and
            levels['source'] == 'native-play-once' and levels['scope'] == 'final-pcm16-all-channels' and
            levels['record_sha256'] == variant['record_sha256'],
            'Generated levels differ from their canonical identity or measurement schema.')
    require(all(type(levels[key]) is int and 0 < levels[key] <= 0xffffffff
                for key in ('sample_rate_hz', 'frames', 'channels')) and
            levels['frames'] * levels['channels'] <= 0xffffffff and
            levels['channels'] == variant['technical']['channels'] and
            levels['frames'] / levels['sample_rate_hz'] == variant['duration_seconds'],
            'Generated levels have invalid sample context.')
    require(type(levels['native_clipped']) is int and 0 <= levels['native_clipped'] <= 0xffffffff and
            type(levels['sample_peak_lsb']) is int and 0 <= levels['sample_peak_lsb'] <= 32768,
            'Generated levels have invalid native counters.')
    if levels['sample_peak_lsb'] == 0:
        require(levels['sample_peak_dbfs'] is None and levels['rms_dbfs'] is None,
                'Measured digital silence requires null peak/RMS dBFS.')
    else:
        require(all(type(levels[key]) in (int, float) and math.isfinite(levels[key]) and levels[key] <= 0
                    for key in ('sample_peak_dbfs', 'rms_dbfs')) and
                levels['rms_dbfs'] <= levels['sample_peak_dbfs'],
                'Generated levels have invalid peak/RMS dBFS.')


def levels_from_play(play, variant):
    # Native PLAY rounds dBFS to two decimals. Preserve those reported values;
    # do not invent precision or recalculate PCM in this exporter.
    levels = dict(source='native-play-once', scope='final-pcm16-all-channels',
                  record_sha256=variant['record_sha256'], sample_rate_hz=int(play['fs']),
                  frames=int(play['samples']), channels=int(play.get('channels', '1')),
                  sample_peak_lsb=int(play['peak']), native_clipped=int(play['clipped']))
    for target, source in (('sample_peak_dbfs', 'peak_dbfs'), ('rms_dbfs', 'rms_dbfs')):
        levels[target] = None if play[source] == '-inf' else float(play[source])
    validate_levels(levels, variant)
    return levels


def render_variant(task, directory, harness):
    song, kind, filename, paths = task
    target = directory / filename
    headers = [document.parse_document(path.read_bytes()) for path in paths]
    portable = [parsed for parsed in headers if parsed['header']]
    require(len(portable) <= 1, 'A merged variant may carry only one portable metadata header.')
    body_target = directory / (filename + '.body')
    command = [str(harness), '--play-once', '--merged', str(target)]
    if portable:
        command += ['--merged-body', str(body_target)]
    result = subprocess.run([*command, '--', *map(str, paths)],
                            capture_output=True, text=True, timeout=600)
    require(result.returncode == 0, f'Native export refused {song}/{kind} (exit {result.returncode}): {result.stderr.strip()[:400]}')
    rows = [line for line in result.stdout.splitlines() if line.startswith('PLAY ')]
    require(len(rows) == 1, 'Native renderer did not return exactly one PLAY row.')
    play = dict(item.split('=', 1) for item in rows[0].split()[1:])
    require(play.get('match') == 'yes' and play.get('block_indep') == 'unchecked' and
            re.fullmatch('[a-f0-9]{16}', play.get('expected', '')) and play.get('expected') == play.get('hash'),
            f'Saved hash was not checked successfully for {song}/{kind}.')
    fs, samples = int(play['fs']), int(play['samples'])
    require(fs > 0 and samples > 0 and target.is_file(), 'Native export returned no complete record.')
    inspected = subprocess.run([str(harness), '--inspect', '--', str(target)], capture_output=True, text=True, timeout=60)
    require(inspected.returncode == 0, 'Native inspection refused exported record: ' + song + '/' + kind)
    rows = [line for line in inspected.stdout.splitlines() if line.startswith('INSPECT ')]
    require(len(rows) == 1, 'Native inspector did not return exactly one INSPECT row.')
    info = dict(item.split('=', 1) for item in rows[0].split()[1:])
    require(info.get('hash_present') == '1' and info.get('expected_hash') == play['hash'] and
            int(info['fs']) == fs and int(info['frames']) == samples, 'Native inspection disagrees with exported render.')
    data = target.read_bytes()
    parsed = document.parse_document(data)
    require(parsed['header'] == (portable[0]['header'] if portable else b''),
            'Native full export changed the portable metadata header.')
    if portable:
        require(body_target.is_file() and body_target.read_bytes() == parsed['body'],
                'Native body-only export differs from full-document music body.')
        body_target.unlink()
    technical = dict(source='native-inspect', channels=int(info['channels']), route=int(info['route']),
                     allocated_voices=int(info['voices']), events=int(info['events']))
    variant = dict(kind=kind, path='songs/' + filename, bytes=target.stat().st_size, duration_seconds=samples / fs,
                   record_sha256=hashlib.sha256(parsed['body']).hexdigest(), technical=technical)
    if portable:
        variant['document_sha256'] = hashlib.sha256(data).hexdigest()
    variant['levels'] = levels_from_play(play, variant)
    return song, variant


def browser_songs(manifest):
    """Project the complete archive into website discovery without changing it.

    The Original (standard) must exceed 20 seconds; short saved profiles are
    also omitted. There is no upper limit for existing library recordings.
    songs.json and canonical TL files retain every archived composition/profile.
    """
    songs = []
    for song in manifest['songs']:
        variants = song['variants']
        require(all(type(v['duration_seconds']) in (int, float) and
                    math.isfinite(v['duration_seconds']) and v['duration_seconds'] > 0 for v in variants),
                'Browser durations must be positive native durations.')
        original = next(v for v in variants if v['kind'] == 'standard')
        if original['duration_seconds'] > 20:
            songs.append({**song, 'variants': [v for v in variants if v['duration_seconds'] > 20]})
    return songs


def catalog_summary(manifest):
    """Describe the browsable source library, never exchange activity or quality.

    Standard represents each logical composition once. Other profile metadata
    contributes only to separate version coverage. Exact variant identities and
    declarations remain in songs.json; this small summary contains no song rows.
    Counts match website discovery; snapshot identity still binds all songs.json.
    """
    songs = browser_songs(manifest)
    standards = [next(v for v in song['variants'] if v['kind'] == 'standard') for song in songs]
    variants = [v for song in songs for v in song['variants']]

    def declaration(variant, field):
        discovery = variant.get('discovery', {})
        if field == 'model':
            provenance = discovery.get('provenance', {})
            return ((provenance['model'], provenance['model_source']) if provenance.get('model') else None)
        value = discovery.get('metadata', {}).get(field)
        if not value:
            return None
        return ((value['min_bpm'], value['max_bpm']) if field == 'tempo' else value['value'], value['source'])

    def coverage(rows, field):
        known = sum(declaration(row, field) is not None for row in rows)
        return dict(denominator=len(rows), known=known, unknown=len(rows) - known)

    def breakdown(counts, *, tempo=False):
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        selected = ranked[:SUMMARY_BREAKDOWN_LIMIT]
        items = [dict(min_bpm=value[0], max_bpm=value[1], count=count) if tempo else
                 dict(value=value, count=count) for value, count in selected]
        return dict(limit=SUMMARY_BREAKDOWN_LIMIT, total_labels=len(ranked), items=items,
                    omitted_labels=max(0, len(ranked) - len(selected)),
                    omitted_count=sum(count for _, count in ranked[SUMMARY_BREAKDOWN_LIMIT:]))

    declarations = {}
    for field in ('genre', 'tempo', 'model'):
        counts, sources = {}, {}
        for variant in standards:
            value = declaration(variant, field)
            if value is not None:
                label, source = value
                counts[label] = counts.get(label, 0) + 1
                sources[source] = sources.get(source, 0) + 1
        result = {**coverage(standards, field),
                  'by_source': [dict(source=source, count=count) for source, count in sorted(sources.items())],
                  'breakdown': breakdown(counts, tempo=field == 'tempo')}
        if field == 'tempo':
            fixed = sum(count for (low, high), count in counts.items() if low == high)
            result.update(beat_unit='quarter-note', fixed=fixed, ranged=result['known'] - fixed,
                          min_bpm=min((low for low, _ in counts), default=None),
                          max_bpm=max((high for _, high in counts), default=None))
        declarations[field] = result

    durations = sorted(v['duration_seconds'] for v in standards)
    require(all(type(d) in (int, float) and math.isfinite(d) and d > 0 for d in durations),
            'Summary durations must be positive native durations.')
    count = len(durations)
    total = math.fsum(durations)
    median = ((durations[(count - 1) // 2] + durations[count // 2]) / 2) if count else None
    edges = (0, 30, 60, 120, 180, 300, None)
    distribution = [dict(min_inclusive=low, max_exclusive=high,
                         count=sum(low <= d and (high is None or d < high) for d in durations))
                    for low, high in zip(edges, edges[1:])]
    # Functional snapshot identity: the same manifest gives the same summary.
    # No build time, session identity, private input or separate evidence ledger.
    identity = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return dict(version=1, scope='source-library', as_of=dict(source='songs.json', sha256=identity),
                selection='standard', compositions=len(songs), canonical_versions=len(variants),
                duration_seconds=dict(denominator=count, known=count, unknown=0, total=total,
                                      mean=total / count if count else None, median=median,
                                      min=min(durations, default=None), max=max(durations, default=None),
                                      distribution=distribution),
                declarations=declarations,
                version_coverage={field: coverage(variants, field) for field in ('genre', 'tempo', 'model')})


def catalog_documents(manifest):
    """Small static browsing pages; references existing TLs, never copies audio."""
    def item(song, variant, note=None):
        result = {key: song[key] for key in ('title', 'collection', 'revision')}
        result['id'] = variant['id']
        result.update(variant=variant['kind'], **{key: variant[key] for key in
                      ('path', 'bytes', 'record_sha256', 'duration_seconds', 'technical')})
        if 'document_sha256' in variant:
            result['document_sha256'] = variant['document_sha256']
        if 'levels' in variant:
            result['levels'] = variant['levels']
        discovery = variant.get('discovery', {})
        for key in ('metadata', 'provenance'):
            if key in discovery:
                result[key] = discovery[key]
        result['variants'] = [{key: saved[key] for key in
                               ('kind', 'id', 'path', 'bytes', 'duration_seconds', 'record_sha256',
                                'technical', 'discovery', 'levels', 'document_sha256') if key in saved}
                              for saved in song['variants']]
        if note:
            result['note'] = note
        return result

    # Sort the complete browsing projection before category selection/pagination.
    # Keep the archive and its summary identity in their original source order.
    normalized = lambda text: unicodedata.normalize('NFKC', text).casefold()
    songs = sorted(browser_songs(manifest),
                   key=lambda song: (normalized(song['title']), normalized(song['collection']), song['id']))
    selected = [item(song, next((v for v in song['variants'] if v['kind'] == 'keys'),
                               next(v for v in song['variants'] if v['kind'] == 'standard')))
                for song in songs]
    sections = []
    for key, title in [('featured', 'Favorites'), ('showcase', 'Synth showcase')]:
        ranked = [(v['discovery'][key]['order'], item(song, v, v['discovery'][key]['note']))
                  for song in songs for v in song['variants'] if key in v.get('discovery', {})]
        sections.append((key, title, [row for _, row in sorted(ranked, key=lambda pair: pair[0])]))
    sections.append(('all', 'Browse all', selected))
    for index, name in enumerate(sorted({row['collection'] for row in selected}), 1):
        sections.append(('collection-' + str(index), name,
                         [row for row in selected if row['collection'] == name]))
    # Exact-variant declarations only: a label never transfers to another profile.
    for field, title in [('genre', 'Genre'), ('model', 'Model')]:
        groups = {}
        for song in songs:
            seen = set()
            for variant in song['variants']:
                discovery = variant.get('discovery', {})
                value = (discovery.get('metadata', {}).get('genre', {}).get('value') if field == 'genre'
                         else discovery.get('provenance', {}).get('model'))
                if value and value not in seen:
                    groups.setdefault(value, []).append(item(song, variant))
                    seen.add(value)
        for index, (name, rows) in enumerate(sorted(groups.items()), 1):
            sections.append((field + '-' + str(index), title + ': ' + name, rows))
    documents, categories = {}, []
    for category, title, rows in sections:
        page_path = lambda number: 'catalog/' + category + '-' + str(number) + '.json'
        categories.append(dict(id=category, title=title, count=len(rows),
                               first_page=page_path(1) if rows else None))
        for start in range(0, len(rows), CATALOG_PAGE_SIZE):
            number = start // CATALOG_PAGE_SIZE + 1
            documents[page_path(number)] = dict(version=1, category=category, total=len(rows),
                items=rows[start:start + CATALOG_PAGE_SIZE],
                next=page_path(number + 1) if start + CATALOG_PAGE_SIZE < len(rows) else None)
    documents['catalog/index.json'] = dict(version=1, page_size=CATALOG_PAGE_SIZE,
                                          total_songs=len(songs), categories=categories,
                                          summary='catalog/summary.json')
    documents['catalog/summary.json'] = catalog_summary(manifest)
    encoded = {name: (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
               for name, value in documents.items()}
    require(all(len(data) <= CATALOG_JSON_LIMIT for data in encoded.values()),
            'A browsing catalog document exceeds the 256 KiB client limit.')
    return encoded


def write_catalog(staged, manifest):
    (staged / 'catalog').mkdir()
    for name, data in catalog_documents(manifest).items():
        (staged / name).write_bytes(data)


def publish(staged, output, names=('songs', 'songs.json', 'catalog')):
    """Replace generated files after all renders pass; roll back I/O errors."""
    moved, installed = [], []
    try:
        for name in names:
            target = output / name
            if target.exists():
                target.rename(staged / ('previous-' + name))
                moved.append(name)
        for name in names:
            (staged / name).rename(output / name)
            installed.append(name)
    except BaseException:
        for name in reversed(installed):
            target = output / name
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        for name in reversed(moved):
            (staged / ('previous-' + name)).rename(output / name)
        raise


def protect_inputs(catalog, value, tasks, root, targets):
    inputs = [Path(catalog).resolve(), *(source for task in tasks for source in task[3])]
    inputs += [(Path(root) / variant['discovery']['source']).resolve()
               for song in value['songs'] for variant in song['variants'] if 'discovery' in variant]
    require(all(not source.is_relative_to(target) for source in inputs for target in targets),
            'Export would replace its own sources.')


def export_library(catalog, output, harness, *, root=ROOT, jobs=1):
    root, output, harness = Path(root).resolve(), Path(output).resolve(), Path(harness).resolve()
    require(type(jobs) is int and jobs > 0, '--jobs must be a positive integer.')
    value, tasks = catalog_entries(catalog, root)
    for name in ('songs', 'songs.json', 'catalog'):
        require(not (output / name).is_symlink(), 'Refuse a symlink export target.')
    protect_inputs(catalog, value, tasks, root, [output / name for name in ('songs', 'songs.json', 'catalog')])
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.songs-export-', dir=output) as directory:
        staged = Path(directory)
        (staged / 'songs').mkdir()
        results = {}
        with ThreadPoolExecutor(max_workers=jobs) as workers:
            futures = [workers.submit(render_variant, task, staged / 'songs', harness) for task in tasks]
            try:
                for future in as_completed(futures):
                    song, variant = future.result()
                    results[(song, variant['kind'])] = variant
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        for song in value['songs']:
            for variant in song['variants']:
                result = results[(song['id'], variant['kind'])]
                result['id'] = variant['id']
                discovery = resolve_discovery(song, variant, (staged / result['path']).read_bytes(),
                                              Path(catalog).resolve().relative_to(root).as_posix())
                if discovery is not None:
                    require(discovery['record_sha256'] == result['record_sha256'],
                            'Discovery identity does not match canonical variant: ' + song['id'] + '/' + variant['kind'])
                    result['discovery'] = discovery
        songs = [{**{key:song[key] for key in ('id','title','collection')},
                  'revision':song.get('revision', 'Published'),
                  'variants':[results[(song['id'], variant['kind'])] for variant in song['variants']]}
                 for song in value['songs']]
        manifest = {'version':1, 'songs':songs}
        (staged / 'songs.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        write_catalog(staged, manifest)
        publish(staged, output)
    return manifest


def check_output(catalog, output, *, root=ROOT, check_browse=True, check_discovery=True):
    """Check a complete generated export without rendering or parsing TL grammar."""
    root, output = Path(root).resolve(), Path(output).resolve()
    source, tasks = catalog_entries(catalog, root)
    require(not (output / 'songs.json').is_symlink() and not (output / 'songs').is_symlink(),
            'Refuse a symlink export target.')
    manifest = json.loads((output / 'songs.json').read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    require(isinstance(manifest, dict) and set(manifest) == {'version', 'songs'} and
            type(manifest['version']) is int and manifest['version'] == 1 and
            isinstance(manifest['songs'], list) and len(manifest['songs']) == len(source['songs']),
            'Generated song catalog is incomplete or invalid.')
    filenames = {(song, kind):filename for song, kind, filename, _ in tasks}
    source_headers = {(song, kind):[document.parse_document(path.read_bytes())['header'] for path in paths]
                      for song, kind, _, paths in tasks}
    require((output / 'songs').is_dir() and {path.name for path in (output / 'songs').iterdir()} == set(filenames.values()),
            'Generated song files are incomplete or unexpected.')
    for song, generated in zip(source['songs'], manifest['songs']):
        expected = {key:song[key] for key in ('id', 'title', 'collection')}
        expected['revision'] = song.get('revision', 'Published')
        require(isinstance(generated, dict) and set(generated) == {*expected, 'variants'} and
                all(generated[key] == value for key, value in expected.items()) and
                isinstance(generated['variants'], list) and len(generated['variants']) == len(song['variants']),
                'Generated song metadata or variants differ from the source catalog.')
        for variant, generated_variant in zip(song['variants'], generated['variants']):
            fields = {'kind', 'id', 'path', 'bytes', 'duration_seconds', 'record_sha256', 'technical'}
            path = output / 'songs' / filenames[(song['id'], variant['kind'])]
            require(not path.is_symlink() and path.is_file(), 'Generated canonical TL is missing or a symlink.')
            data = path.read_bytes()
            parsed = document.parse_document(data)
            headers = [header for header in source_headers[(song['id'], variant['kind'])] if header]
            require(len(headers) <= 1 and parsed['header'] == (headers[0] if headers else b''),
                    'Generated portable header differs from its source document.')
            expected_discovery = resolve_discovery(song, variant, data, Path(catalog).resolve().relative_to(root).as_posix())
            if isinstance(generated_variant, dict) and (expected_discovery is not None if check_discovery else 'discovery' in generated_variant):
                fields.add('discovery')
            if parsed['header']:
                fields.add('document_sha256')
            require(isinstance(generated_variant, dict) and fields <= set(generated_variant) <= fields | {'levels'} and
                    generated_variant['kind'] == variant['kind'] and generated_variant['id'] == variant['id'] and
                    generated_variant['path'] == 'songs/' + filenames[(song['id'], variant['kind'])] and
                    (not check_discovery or generated_variant.get('discovery') == expected_discovery),
                    'Generated variant identity or discovery differs from the source catalog.')
            path = output / generated_variant['path']
            require(not path.is_symlink() and path.is_file(), 'Generated canonical TL is missing or a symlink.')
            data = path.read_bytes()
            require(type(generated_variant['bytes']) is int and generated_variant['bytes'] == len(data) and
                    generated_variant['record_sha256'] == hashlib.sha256(parsed['body']).hexdigest() and
                    ('discovery' not in variant or variant['discovery']['record_sha256'] == generated_variant['record_sha256']),
                    'Generated canonical TL size or SHA-256 differs from its catalog identity.')
            if parsed['header']:
                require(generated_variant['document_sha256'] == hashlib.sha256(data).hexdigest(),
                        'Generated portable TL full-file checksum differs.')
            if 'discovery' in generated_variant:
                validate_discovery(generated_variant['discovery'], root)
                require(generated_variant['discovery']['record_sha256'] == generated_variant['record_sha256'],
                        'Stored discovery differs from its canonical identity.')
            duration = generated_variant['duration_seconds']
            require(type(duration) in (int, float) and math.isfinite(duration) and duration > 0,
                    'Generated duration must be a positive finite number.')
            technical = generated_variant['technical']
            require(isinstance(technical, dict) and set(technical) == {'source', 'channels', 'route', 'allocated_voices', 'events'} and
                    technical['source'] == 'native-inspect' and
                    all(type(technical[key]) is int for key in ('channels', 'route', 'allocated_voices', 'events')) and
                    technical['route'] in (0, 1, 2, 3, 4) and
                    technical['channels'] in ({0:(1, 2), 1:(4,), 2:(16,), 3:(2,), 4:(2,)}[technical['route']]) and
                    technical['allocated_voices'] > 0 and technical['events'] >= 0,
                    'Generated native inspection fields are invalid.')
            if 'levels' in generated_variant:
                validate_levels(generated_variant['levels'], generated_variant)
    if check_browse:
        expected = catalog_documents(manifest)
        directory = output / 'catalog'
        require(not directory.is_symlink() and directory.is_dir(), 'Generated browsing catalog is missing or a symlink.')
        actual = {'catalog/' + p.name for p in directory.iterdir()}
        require(actual == set(expected), 'Generated browsing pages are incomplete or unexpected.')
        for name, data in expected.items():
            file = output / name
            require(not file.is_symlink() and file.is_file() and file.read_bytes() == data,
                    'Generated browsing page differs from the validated song catalog.')
    return manifest


def refresh_catalog(catalog, output, *, root=ROOT):
    output = Path(output).resolve()
    value, tasks = catalog_entries(catalog, Path(root).resolve())
    protect_inputs(catalog, value, tasks, root, [output / 'catalog'])
    manifest = check_output(catalog, output, root=root, check_browse=False)
    require(not (output / 'catalog').is_symlink(), 'Refuse a symlink browsing catalog.')
    with tempfile.TemporaryDirectory(prefix='.catalog-export-', dir=output) as directory:
        staged = Path(directory)
        write_catalog(staged, manifest)
        publish(staged, output, names=('catalog',))
    return manifest


def refresh_metadata(catalog, output, *, root=ROOT):
    """Apply hash-bound discovery edits to a checked export without rendering.

    Structural source changes, missing/corrupt TLs and stale discovery hashes
    refuse before either generated JSON product is replaced. Canonical TL files,
    measurements, registry and service worker are never written.
    """
    root, output = Path(root).resolve(), Path(output).resolve()
    value, tasks = catalog_entries(catalog, root)
    targets = [output / name for name in ('songs.json', 'catalog')]
    require(all(not target.is_symlink() for target in targets), 'Refuse a symlink metadata target.')
    protect_inputs(catalog, value, tasks, root, targets)
    manifest = check_output(catalog, output, root=root, check_browse=False, check_discovery=False)
    for song, generated in zip(value['songs'], manifest['songs']):
        for variant, generated_variant in zip(song['variants'], generated['variants']):
            discovery = resolve_discovery(song, variant, (output / generated_variant['path']).read_bytes(),
                                          Path(catalog).resolve().relative_to(root).as_posix())
            if discovery is not None:
                generated_variant['discovery'] = discovery
            else:
                generated_variant.pop('discovery', None)
    with tempfile.TemporaryDirectory(prefix='.metadata-export-', dir=output) as directory:
        staged = Path(directory)
        (staged / 'songs.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        write_catalog(staged, manifest)
        publish(staged, output, names=('songs.json', 'catalog'))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'music/library.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'web', help='asset directory receiving songs/ and songs.json')
    parser.add_argument('--harness', type=Path, default=ROOT / 'build/s12e10p1v1/harness')
    parser.add_argument('--root', type=Path, default=ROOT, help='root for catalog source paths')
    parser.add_argument('--jobs', type=int, default=1)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--list-inputs', action='store_true', help='print source prerequisites for Make without rendering')
    action.add_argument('--check-output', action='store_true', help='check complete generated assets without rendering')
    action.add_argument('--refresh-catalog', action='store_true', help='rebuild browsing pages from a checked existing export without rendering')
    action.add_argument('--refresh-metadata', action='store_true', help='apply exact-identity discovery edits and rebuild pages without rendering')
    args = parser.parse_args()
    try:
        if args.list_inputs:
            value, tasks = catalog_entries(args.catalog, args.root.resolve())
            sources = {source for task in tasks for source in task[3]}
            sources.update((args.root / variant['discovery']['source']).resolve()
                           for song in value['songs'] for variant in song['variants'] if 'discovery' in variant)
            for source in sorted(sources):
                print(str(source).replace('$', '$$').replace('#', '\\#').replace(' ', '\\ '))
            return
        if args.check_output:
            check_output(args.catalog, args.output, root=args.root)
            print('Complete generated song catalog, canonical files and discovery identities match.')
            return
        if args.refresh_catalog:
            refresh_catalog(args.catalog, args.output, root=args.root)
            print('Refreshed bounded browsing pages; canonical TL files are unchanged.')
            return
        if args.refresh_metadata:
            refresh_metadata(args.catalog, args.output, root=args.root)
            print('Refreshed source metadata and bounded browsing pages; canonical TL files are unchanged.')
            return
        manifest = export_library(args.catalog, args.output, args.harness, root=args.root, jobs=args.jobs)
        print(f'Exported {len(manifest["songs"])} songs / {sum(len(song["variants"]) for song in manifest["songs"])} standalone TL files; saved hashes match; block independence unchecked.')
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
