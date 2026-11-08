#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Bounded release-package lifecycle checks; no compiler or network required."""
import importlib.util
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_web', ROOT / 'tools/package_web.py')
pkg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pkg)
REAL_PUBLISHED = pkg.published_commit
REPO = 'https://example.invalid/jottune-synth'


class Packages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jottune-package-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {
            'LICENSE': 'Fixture project licence\n',
            'web/runtime.mjs': "import './vendor/browser_wasi_shim/index.js';\n",
            'web/vendor/browser_wasi_shim/index.js': 'export const wasi = true;\n',
            'web/vendor/browser_wasi_shim/LICENSE': 'fixture licence',
            'web/sw.template.js': "// Worker source fixture\nconst RELEASE='development';\n",
            'web/sw.js': "// Previous generated worker\nconst RELEASE='installed';\n",
            'docs/INSTRUMENT.md': '# Instrument\n',
            'tests/golden/48000/sine.tl': 'fixture only\n',
            **{'build/wasm/' + name: name for name in pkg.MODULES},
            'notices/wasi-libc-LICENSE': 'fixture licence',
            'notices/compiler-rt-LICENSE': 'fixture licence',
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        self.calls = patch.object(pkg.subprocess, 'check_output', side_effect=lambda command, **_: (
            'ENGINE test-engine\n' if command[-1] == '--engine' else 'PARAM fixture\n'))
        self.calls.start()
        self.addCleanup(self.calls.stop)
        self.identities = patch.object(pkg, 'wasm_identities', return_value={
            'harness.wasm': 'test-engine', 'browser.wasm': 'test-engine'})
        self.identity_mock = self.identities.start()
        self.addCleanup(self.identities.stop)
        self.published = patch.object(pkg, 'published_commit', return_value=True)
        self.published.start()
        self.addCleanup(self.published.stop)

    def stamp(self, revision):
        pkg.write_json(self.root / 'build/wasm/build.json', {'source_commit':revision,
            'toolchain':'fixture cc', 'artifacts':{name:pkg.digest(self.root / 'build/wasm' / name)
                for name in pkg.MODULES}})

    def agent(self):
        for name in ('agent.mjs','worker.mjs','policy.mjs','media.mjs','links.mjs','analysis-worker.mjs'):
            (self.root / 'web' / name).write_text('// fixture static module\n')

    def test_agent_manifest_binds_all_transitive_assets_and_excludes_rewritten_and_bulk_content(self):
        self.agent()
        (self.root / 'web/starter.tl').write_text('fixture editable starter')
        for name in ('index.html','theme.css','songs.json','precache.json'):
            (self.root / 'web' / name).write_text('not a static client dependency')
        for name in ('songs/song.tl','catalog/index.json'):
            file = self.root / 'web' / name
            file.parent.mkdir(exist_ok=True)
            file.write_text('on-demand only')
        release = pkg.package(self.root)
        manifest = json.loads((self.root / 'web/agent-manifest.json').read_text())
        entries = {entry['path']:entry for entry in manifest['files']}
        self.assertEqual(manifest['format'], 1)
        self.assertEqual(manifest['entry'], 'agent.mjs')
        for name in ('agent.mjs','analysis-worker.mjs','runtime.mjs','vendor/browser_wasi_shim/index.js',
                     'vendor/browser_wasi_shim/LICENSE','LICENSE','engines.json','starter.tl','example.tl',release['runtime_module'],
                     'engines/' + release['id'] + '/vendor/browser_wasi_shim/index.js'):
            self.assertEqual(entries[name]['sha256'], pkg.digest(self.root / 'web' / name))
            self.assertEqual(entries[name]['bytes'], (self.root / 'web' / name).stat().st_size)
        for name in ('index.html','theme.css','songs.json','precache.json','sw.js','sw.template.js','songs/song.tl','catalog/index.json'):
            self.assertNotIn(name, entries)
        self.assertNotIn('sw.template.js', json.loads((self.root / 'web/precache.json').read_text()))
        before = (self.root / 'web/agent-manifest.json').read_bytes()
        (self.root / 'web/index.html').write_text('Worker rewrote markup')
        pkg.refresh_assets(self.root)
        self.assertEqual((self.root / 'web/agent-manifest.json').read_bytes(), before)
        (self.root / 'web/vendor/browser_wasi_shim/index.js').write_text('export const changed = true;')
        pkg.refresh_assets(self.root)
        self.assertNotEqual((self.root / 'web/agent-manifest.json').read_bytes(), before)
        (self.root / 'web' / release['runtime_module']).write_text('modified kept dependency')
        with self.assertRaisesRegex(ValueError, 'differs from registry'):
            pkg.refresh_assets(self.root)

    def test_hosted_references_resolve_local_links_and_cite_source_only_paths(self):
        for source in ('tools/README.md','README.md','docs/INTERCHANGE.md','data/recipes.txt','data/tables.txt'):
            file = self.root / source
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('Fixture reference\n')
        for name in ('songs.json','browser-guide.txt','agent-start.txt','authoring.txt','analysis.txt'):
            (self.root / 'web' / name).write_text('fixture hosted reference')
        text = '''# Instrument
[package](../tools/README.md) [library](../examples/library.json)
[build](../README.md) [interchange](INTERCHANGE.md)
[recipes](../data/recipes.txt) [tables](../data/tables.txt)
[browser](../web/browser-guide.txt) [start](../web/agent-start.txt)
[authoring](../web/authoring.txt) [analysis](../web/analysis.txt)
[source](../src/host/harness.c) [external](https://example.invalid/guide)
## Native instrument and timeline reference
[native source](../src/host/fm_a1.h) [elsewhere](#host-section)
'''
        (self.root / 'docs/INSTRUMENT.md').write_text(text)
        pkg.refresh_assets(self.root)
        hosted = (self.root / 'web/instrument.txt').read_text()
        for target in ('local-tools.txt','songs.json','project.txt','interchange.txt','reference/recipes.txt',
                       'reference/tables.txt','browser-guide.txt','agent-start.txt','authoring.txt','analysis.txt'):
            self.assertIn('](' + target + ')', hosted)
            self.assertTrue((self.root / 'web' / target).is_file())
        self.assertNotIn('](../', hosted)
        self.assertIn('source checkout: `src/host/harness.c`', hosted)
        self.assertIn('](https://example.invalid/guide)', hosted)
        native = (self.root / 'web/native-reference.txt').read_text()
        self.assertTrue(native.startswith('## Native instrument and timeline reference'))
        self.assertIn('](instrument.txt#host-section)', native)
        self.assertFalse((self.root / 'web/sharing.mjs').exists())
        self.assertFalse((self.root / 'web/openapi.json').exists())
        self.assertFalse((self.root / 'site').exists())

    def test_optional_analysis_companion_requires_exact_build_and_registry_binding(self):
        web, build = self.root / 'web', self.root / 'build/wasm'
        registry = {'format':1,'default':'kept','engines':[{'id':'kept',
            'browser_sha256':pkg.digest(build / 'browser.wasm'),'library_sha256':pkg.digest(build / 'library.wasm')}]}
        pkg.write_json(web / 'engines.json', registry)
        original_registry, original_sw = (web / 'engines.json').read_bytes(), (web / 'sw.js').read_bytes()
        pkg.refresh_assets(self.root)
        self.assertFalse((web / 'analysis-view.wasm').exists())
        (build / 'analysis.wasm').write_bytes(b'optional native analysis')
        with self.assertRaisesRegex(ValueError, 'optional native analysis'):
            pkg.refresh_assets(self.root)
        metadata = {'format':1, **{key:pkg.digest(build / name) for key,name in (
            ('wasm_sha256','analysis.wasm'),('browser_sha256','browser.wasm'),('library_sha256','library.wasm'))}}
        pkg.write_json(build / 'analysis.json', metadata)
        pkg.refresh_assets(self.root)
        self.assertEqual((web / 'analysis-view.wasm').read_bytes(), b'optional native analysis')
        self.assertEqual((web / 'engines.json').read_bytes(), original_registry)
        self.assertEqual((web / 'sw.js').read_bytes(), original_sw)
        for name in ('analysis.wasm','browser.wasm','library.wasm'):
            file = build / name
            before = file.read_bytes()
            file.write_bytes(before + b'changed')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                pkg.refresh_assets(self.root)
            file.write_bytes(before)
        registry['engines'][0]['browser_sha256'] = '0' * 64
        pkg.write_json(web / 'engines.json', registry)
        with self.assertRaisesRegex(ValueError, 'installed renderer'):
            pkg.refresh_assets(self.root)

    def test_agent_manifest_rejects_symlinks_and_retains_available_historical_files(self):
        self.agent()
        entry = pkg.package(self.root)
        registry_path = self.root / 'web/engines.json'
        registry = json.loads(registry_path.read_text())
        registry['engines'].append({'id':'withdrawn-missing','withdrawn':True,'runtime_module':'engines/withdrawn-missing/runtime.mjs'})
        pkg.write_json(registry_path, registry)
        pkg.refresh_assets(self.root)
        entries = json.loads((self.root / 'web/agent-manifest.json').read_text())['files']
        self.assertIn(entry['runtime_module'], [item['path'] for item in entries])
        self.assertNotIn('engines/withdrawn-missing/runtime.mjs', [item['path'] for item in entries])
        (self.root / 'web/evil.mjs').symlink_to(self.root / 'LICENSE')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            pkg.refresh_assets(self.root)

    def test_development_archive_includes_runtime_and_licence(self):
        template = (self.root / 'web/sw.template.js').read_text()
        first = pkg.package(self.root)
        worker = (self.root / 'web/sw.js').read_text()
        self.assertRegex(worker, r"const RELEASE='\d{8}T\d{12}Z';")
        self.assertEqual(re.sub(r"const RELEASE='[^']+';", "const RELEASE='development';", worker), template)
        self.assertEqual((self.root / 'web/sw.template.js').read_text(), template)
        self.assertTrue(first['development'])
        self.assertIsNone(first['release_id'])
        self.assertIsNone(first['source_commit'])
        self.assertTrue(first['id'].startswith('dev-'))
        archive = self.root / 'web/engines' / first['id']
        self.assertEqual((archive / 'LICENSE').read_bytes(), (self.root / 'LICENSE').read_bytes())
        self.assertTrue((archive / 'vendor/browser_wasi_shim/LICENSE').is_file())
        self.assertEqual(pkg.package(self.root)['id'], first['id'])
        self.assertEqual((self.root / 'web/sw.template.js').read_text(), template)
        (self.root / 'web/runtime.mjs').write_text('export const changed = true;\n')
        self.assertNotEqual(pkg.package(self.root)['id'], first['id'])
        self.assertIn('browser_wasi_shim', (archive / 'runtime.mjs').read_text())

    def test_asset_refresh_preserves_kept_registry_and_existing_service_worker(self):
        web = self.root / 'web'
        registry = b'{"default":"kept","engines":[]}\n'
        (web / 'engines.json').write_bytes(registry)
        before = (web / 'sw.js').read_bytes()
        fonts = web / 'fonts'
        fonts.mkdir()
        (fonts / 'panel.woff2').write_bytes(b'font fixture')
        pkg.refresh_assets(self.root)
        self.assertEqual((fonts / 'panel.woff2').read_bytes(), b'font fixture')
        (self.root / 'docs/INSTRUMENT.md').write_text('# Updated instrument guide\n')
        pkg.refresh_assets(self.root)
        self.assertEqual((web / 'instrument.txt').read_text(), '# Updated instrument guide\n')
        self.assertEqual((web / 'engines.json').read_bytes(), registry)
        self.assertEqual((web / 'sw.js').read_bytes(), before)
        self.assertIn('fonts/panel.woff2', json.loads((web / 'precache.json').read_text()))
        self.assertIn('instrument.txt', json.loads((web / 'precache.json').read_text()))

    def test_project_licence_bytes_are_preserved_and_bind_the_development_package(self):
        first = pkg.package(self.root)
        original = (self.root / 'LICENSE').read_bytes()
        (self.root / 'LICENSE').write_bytes(original + b'Additional fixture notice.\n')
        second = pkg.package(self.root)
        self.assertNotEqual(first['id'], second['id'])
        first_archive = self.root / 'web/engines' / first['id']
        second_archive = self.root / 'web/engines' / second['id']
        self.assertEqual((first_archive / 'LICENSE').read_bytes(), original)
        self.assertEqual((second_archive / 'LICENSE').read_bytes(), (self.root / 'LICENSE').read_bytes())
        (self.root / 'LICENSE').write_text(' \n')
        with self.assertRaisesRegex(ValueError, 'nonempty project LICENSE'):
            pkg.package(self.root)

    def test_spatial_assets_require_matching_build_inputs_without_editing_kept_renderer(self):
        web, build = self.root / 'web', self.root / 'build/wasm'
        (web / 'spatial.mjs').write_text('// Native drawing companion\n')
        registry = b'{"default":"kept","engines":[]}\n'
        (web / 'engines.json').write_bytes(registry)
        before = (web / 'sw.js').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Build the native spatial'):
            pkg.refresh_assets(self.root)
        (build / 'spatial.wasm').write_bytes(b'companion')
        metadata = {'format':1, **{key:pkg.digest(build / name) for key, name in (
            ('wasm_sha256','spatial.wasm'), ('browser_sha256','browser.wasm'),
            ('library_sha256','library.wasm'))}}
        pkg.write_json(build / 'spatial.json', metadata)
        pkg.refresh_assets(self.root)
        self.assertEqual((web / 'spatial-view.wasm').read_bytes(), b'companion')
        self.assertEqual((web / 'engines.json').read_bytes(), registry)
        self.assertEqual((web / 'sw.js').read_bytes(), before)
        self.assertIn('spatial-view.wasm', json.loads((web / 'precache.json').read_text()))
        for name in ('spatial.wasm','browser.wasm','library.wasm'):
            path = build / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                pkg.refresh_assets(self.root)
            path.write_bytes(original)

    def test_clean_checkout_prunes_only_unavailable_development_entries(self):
        def entry(identifier, **fields):
            return dict(id=identifier, wasm=f'engines/{identifier}/harness.wasm',
                        browser_wasm=f'engines/{identifier}/browser.wasm', **fields)
        missing_legacy = entry('0123456789abcdef')
        missing_dev = entry('dev-missing', development=True, release_id=None)
        available = entry('legacy-available')
        for key in ('wasm', 'browser_wasm'):
            path = self.root / 'web' / available[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('existing development artifact')
        missing_runtime = entry('dev-missing-runtime', development=True,
            runtime_module='engines/dev-missing-runtime/runtime.mjs')
        missing_runtime.update(wasm=available['wasm'], browser_wasm=available['browser_wasm'])
        kept = entry('2026-10-24-1234567', release_id='2026-10-24-1234567', development=False)
        withdrawn = entry('2026-10-23-7654321', release_id='2026-10-23-7654321', development=False,
                          withdrawn=True, withdrawn_at='2026-10-25')
        withdrawn_dev = entry('dev-withdrawn', development=True, withdrawn=True, withdrawn_at='2026-10-25')
        rows = [missing_legacy, missing_dev, available, missing_runtime, kept, withdrawn, withdrawn_dev]
        pkg.write_json(self.root / 'web/engines.json', {'format':1, 'default':missing_legacy['id'], 'engines':rows})
        current = pkg.package(self.root)
        registry = json.loads((self.root / 'web/engines.json').read_text())
        self.assertEqual(registry['default'], current['id'])
        self.assertEqual(registry['engines'], [available, kept, withdrawn, withdrawn_dev, current])
        self.assertTrue((self.root / 'web' / current['browser_wasm']).is_file())
        self.assertFalse((self.root / 'web' / kept['browser_wasm']).exists())
        self.assertEqual((self.root / 'web' / available['browser_wasm']).read_text(), 'existing development artifact')

    def test_small_manifest_is_precached_but_paged_catalog_and_songs_are_on_demand(self):
        songs = self.root / 'web/songs'
        songs.mkdir()
        (songs / 'fixture-standard.tl').write_text('complete fixture record')
        (self.root / 'web/songs.json').write_text('{"version":1,"songs":[]}')
        catalog = self.root / 'web/catalog'
        catalog.mkdir()
        (catalog / 'index.json').write_text('{"version":1,"categories":[]}')
        (catalog / 'all-1.json').write_text('{"version":1,"items":[]}')
        pkg.package(self.root)
        precache = json.loads((self.root / 'web/precache.json').read_text())
        self.assertIn('songs.json', precache)
        self.assertNotIn('songs/fixture-standard.tl', precache)
        self.assertFalse(any(item.startswith('catalog/') for item in precache))
        self.assertTrue((catalog / 'index.json').is_file())
        self.assertTrue((songs / 'fixture-standard.tl').is_file())

    def test_notice_bundle_is_allowlisted_nonempty_and_part_of_development_identity(self):
        bare = pkg.package(self.root)
        (self.root / 'notices/unrelated.txt').write_text('unrelated input must not be copied')
        bundled = pkg.package(self.root, notices_dir=self.root / 'notices')
        self.assertNotEqual(bare['id'], bundled['id'])
        destination = self.root / 'web/engines' / bundled['id'] / 'notices'
        self.assertEqual({path.name for path in destination.iterdir()}, set(pkg.NOTICES))
        for name in pkg.NOTICES:
            self.assertEqual((destination / name).read_bytes(), (self.root / 'notices' / name).read_bytes())
        (self.root / 'notices/unrelated.txt').write_text('changed unrelated input')
        self.assertEqual(pkg.package(self.root, notices_dir=self.root / 'notices')['id'], bundled['id'])
        (self.root / 'notices/compiler-rt-LICENSE').write_text(' \n\t')
        before = (self.root / 'web/engines.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            pkg.package(self.root, notices_dir=self.root / 'notices')
        self.assertEqual((self.root / 'web/engines.json').read_bytes(), before)

    def test_fresh_toolchain_notices_are_automatic_for_archives_and_root_companions(self):
        import shutil
        shutil.copytree(self.root / 'notices', self.root / 'build/wasm/notices')
        entry = pkg.package(self.root)
        self.assertIsNotNone(entry['toolchain_notices'])
        for name in pkg.NOTICES:
            expected = (self.root / 'build/wasm/notices' / name).read_bytes()
            self.assertEqual((self.root / 'web/notices' / name).read_bytes(), expected)
            self.assertEqual((self.root / 'web' / entry['toolchain_notices'] / name).read_bytes(), expected)

    def test_kept_source_binding_immutability_and_withdrawal(self):
        revision = '1234567' + 'a' * 33
        release = '2026-10-25-1234567'
        self.stamp(revision)
        with patch.object(pkg, 'source_commit', return_value=revision):
            with self.assertRaisesRegex(ValueError, 'notice'):
                pkg.package(self.root, release_id=release, repository=REPO)
            entry = pkg.package(self.root, release_id=release, notices_dir=self.root / 'notices', repository=REPO)
            self.assertFalse(entry['development'])
            self.assertEqual(entry['release_id'], release)
            self.assertEqual(entry['source_commit'], revision)
            self.identity_mock.assert_called_once_with(self.root.resolve())
            before = (self.root / 'web' / entry['browser_wasm']).read_bytes()
            pkg.withdraw(self.root, release, '2026-10-26')
            again = pkg.package(self.root, release_id=release, notices_dir=self.root / 'notices', repository=REPO)
            self.assertTrue(again['withdrawn'])
            self.assertEqual(again['withdrawn_at'], '2026-10-26')
            self.assertIsNone(json.loads((self.root / 'web/engines.json').read_text())['default'])
            self.assertEqual((self.root / 'web' / entry['browser_wasm']).read_bytes(), before)
            (self.root / 'build/wasm/browser.wasm').write_text('different')
            with self.assertRaisesRegex(ValueError, 'artifacts differ'):
                pkg.package(self.root, release_id=release, notices_dir=self.root / 'notices', repository=REPO)
            self.stamp(revision)
            with self.assertRaisesRegex(ValueError, 'immutable'):
                pkg.package(self.root, release_id=release, notices_dir=self.root / 'notices', repository=REPO)

    def test_kept_toolchain_cannot_be_changed_under_the_same_release(self):
        revision = '1234567' + 'c' * 33
        self.stamp(revision)
        with patch.object(pkg, 'source_commit', return_value=revision):
            pkg.package(self.root, release_id='2026-10-25-1234567', notices_dir=self.root / 'notices', repository=REPO)
            before = (self.root / 'web/engines.json').read_bytes()
            stamp = json.loads((self.root / 'build/wasm/build.json').read_text())
            stamp['toolchain'] = 'different compiler'
            pkg.write_json(self.root / 'build/wasm/build.json', stamp)
            with self.assertRaisesRegex(ValueError, 'toolchain binding'):
                pkg.package(self.root, release_id='2026-10-25-1234567', notices_dir=self.root / 'notices', repository=REPO)
            self.assertEqual((self.root / 'web/engines.json').read_bytes(), before)

    def test_kept_identity_preflight_refuses_either_wasm_mismatch_before_writes(self):
        revision = '1234567' + 'd' * 33
        self.stamp(revision)
        with patch.object(pkg, 'source_commit', return_value=revision):
            for name in ('harness.wasm', 'browser.wasm'):
                self.identity_mock.return_value = {'harness.wasm': 'test-engine', 'browser.wasm': 'test-engine', name: 'other-engine'}
                with self.assertRaisesRegex(ValueError, 'engine identities must match'):
                    pkg.package(self.root, release_id='2026-10-25-1234567', notices_dir=self.root / 'notices', repository=REPO)
                self.assertFalse((self.root / 'web/engines').exists())
                self.assertFalse((self.root / 'web/engines.json').exists())

    def test_kept_release_requires_recorded_toolchain_and_valid_descriptors(self):
        revision = '1234567' + 'e' * 33
        self.stamp(revision)
        stamp = json.loads((self.root / 'build/wasm/build.json').read_text())
        del stamp['toolchain']
        pkg.write_json(self.root / 'build/wasm/build.json', stamp)
        with patch.object(pkg, 'source_commit', return_value=revision):
            with self.assertRaisesRegex(ValueError, 'recorded build toolchain'):
                pkg.package(self.root, release_id='2026-10-25-1234567', notices_dir=self.root / 'notices', repository=REPO)
        self.assertFalse((self.root / 'web/engines').exists())
        for text in ('ENGINE ', 'ENGINE a\nENGINE b', 'invalid', None):
            with self.assertRaisesRegex(ValueError, 'one ENGINE descriptor'):
                pkg.engine_descriptor(text, 'fixture')

    def test_release_rejects_dirty_stale_mismatched_or_missing_source(self):
        revision = '1234567' + 'b' * 33
        with patch.object(pkg, 'source_commit', return_value=None):
            with self.assertRaisesRegex(ValueError, 'source commit'):
                pkg.package(self.root, release_id='2026-10-25-1234567', repository=REPO)
        with patch.object(pkg, 'source_commit', return_value=revision):
            with self.assertRaisesRegex(ValueError, 'Rebuild'):
                pkg.package(self.root, release_id='2026-10-25-1234567', repository=REPO)
            with self.assertRaisesRegex(ValueError, 'must match'):
                pkg.package(self.root, release_id='2026-10-25-7654321', repository=REPO)
        with self.assertRaisesRegex(ValueError, 'requires --release-id'):
            pkg.package(self.root, source=revision)
        with self.assertRaises(ValueError):
            pkg.package(self.root, release_id='../escape', repository=REPO)


    def test_toolchain_notices_carry_cloudlibc_and_musl_texts_from_the_compiler(self):
        lib = self.root / 'zig/lib'
        for name, value in {
            'libc/wasi/LICENSE': 'pointer: cloudlibc is BSD-2-Clause\n',
            'libc/wasi/LICENSE-MIT': 'MIT text\n', 'libc/wasi/LICENSE-APACHE-LLVM': 'Apache LLVM text\n',
            'libc/wasi/libc-bottom-half/cloudlibc/LICENSE': 'BSD-2-Clause cloudlibc text\n',
            'libc/wasi/libc-top-half/musl/COPYRIGHT': 'musl copyright\n',
            'libc/wasi/libc-bottom-half/sources/a.c': 'not a licence\n',
            'libc/musl/COPYRIGHT': 'musl copyright\n', 'LICENSE': 'Zig MIT\n'}.items():
            (lib / name).parent.mkdir(parents=True, exist_ok=True)
            (lib / name).write_text(value)
        notices = pkg.toolchain_notices(lib, lib / 'LICENSE')
        self.assertEqual(set(notices), set(pkg.NOTICES))
        wasi = notices['wasi-libc-LICENSE']
        self.assertTrue(wasi.startswith('libc/wasi/LICENSE\n\npointer'))
        for heading, body in (('libc/wasi/libc-bottom-half/cloudlibc/LICENSE', 'BSD-2-Clause cloudlibc text'),
                              ('libc/wasi/libc-top-half/musl/COPYRIGHT', 'musl copyright'),
                              ('libc/wasi/LICENSE-MIT', 'MIT text')):
            self.assertIn(heading + '\n\n' + body, wasi)
        self.assertNotIn('not a licence', wasi)
        self.assertIn('LICENSE\n\nZig MIT', notices['compiler-rt-LICENSE'])
        (lib / 'libc/wasi/libc-bottom-half/cloudlibc/LICENSE').write_text(' \n')
        with self.assertRaisesRegex(ValueError, 'Empty toolchain licence'):
            pkg.toolchain_notices(lib, lib / 'LICENSE')
        (lib / 'libc/wasi/libc-bottom-half/cloudlibc/LICENSE').unlink()
        with self.assertRaisesRegex(ValueError, 'cloudlibc'):
            pkg.toolchain_notices(lib, lib / 'LICENSE')

    def test_kept_release_names_its_public_repository_and_a_published_commit(self):
        revision = '1234567' + 'f' * 33
        release = '2026-10-25-1234567'
        notices = self.root / 'notices'
        self.stamp(revision)
        with patch.object(pkg, 'source_commit', return_value=revision):
            for bad in (None, 'http://example.invalid/synth', 'https://user@example.invalid/synth',
                        'https://example.invalid/a b', 'git@example.invalid:synth.git', 'https://example.invalid'):
                with self.assertRaisesRegex(ValueError, 'https URL of the public source repository'):
                    pkg.package(self.root, release_id=release, notices_dir=notices, repository=bad)
            with self.assertRaisesRegex(ValueError, 'public ref'):
                pkg.package(self.root, release_id=release, notices_dir=notices, repository=REPO,
                            public_ref='--upload-pack=x')
            with patch.object(pkg, 'published_commit', return_value=False) as published:
                with self.assertRaisesRegex(ValueError, 'not in origin/main'):
                    pkg.package(self.root, release_id=release, notices_dir=notices, repository=REPO)
                published.assert_called_once_with(self.root.resolve(), revision, 'origin/main')
            self.assertFalse((self.root / 'web/engines.json').exists())
            entry = pkg.package(self.root, release_id=release, notices_dir=notices, repository=REPO)
            self.assertEqual((entry['source_commit'], entry['repository']), (revision, REPO))
            with self.assertRaisesRegex(ValueError, 'repository binding'):
                pkg.package(self.root, release_id=release, notices_dir=notices, repository=REPO + '-fork')
        for options in ({'repository': REPO}, {'public_ref': 'origin/main'}):
            with self.assertRaisesRegex(ValueError, 'requires --release-id'):
                pkg.package(self.root, **options)
        self.assertIsNone(pkg.package(self.root)['repository'])

    def test_published_commit_needs_a_remote_tracking_branch_that_contains_it(self):
        import subprocess
        def git(*args):
            return subprocess.run(['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid',
                                   '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                                  cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('commit', '-q', '--allow-empty', '-m', 'one')
        first = git('rev-parse', 'HEAD')
        git('commit', '-q', '--allow-empty', '-m', 'two')
        second = git('rev-parse', 'HEAD')
        self.assertFalse(REAL_PUBLISHED(self.root, first, 'origin/main'))
        git('update-ref', 'refs/remotes/origin/main', first)
        git('branch', 'local-only', second)
        self.assertTrue(REAL_PUBLISHED(self.root, first, 'origin/main'))
        self.assertFalse(REAL_PUBLISHED(self.root, second, 'origin/main'))
        self.assertFalse(REAL_PUBLISHED(self.root, second, 'local-only'))
        self.assertFalse(REAL_PUBLISHED(self.root, 'f' * 40, 'origin/main'))


if __name__ == '__main__':
    unittest.main()
