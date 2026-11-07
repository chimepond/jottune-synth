#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Exercise catalog/search/probe through the same native operations as clients."""
import argparse
import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def legacy_search_checks(analysis, root, check):
    """Preserve four exact legacy comparisons without requiring private history.

    The fixture pins the original implementation and its input bytes. Refresh
    only after reviewing changed inputs and retaining independently derived
    expectations; current output must never silently replace the fixture.
    """
    path = root / 'tests/catalog/search_compat.json'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != '8cff94277f6d6e3158d681c8cf0fcbf1ac6cee587605bd46ebe8f158d54b577c':
        raise AssertionError('legacy search fixture identity differs')
    fixture = json.loads(raw)
    for name, expected in fixture['inputs'].items():
        data = (root / name).read_bytes()
        if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise AssertionError('legacy search fixture input differs: ' + name)
    for index, case in enumerate(fixture['cases']):
        captured, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
            status = analysis.cmd_search(case['query'])
        check('legacy-search-' + str(index), status == 0 and errors.getvalue() == ''
              and captured.getvalue() == case['stdout'])


def legacy_search_refusals(root, out, check):
    fixture = 'tests/catalog/search_compat.json'
    inputs = ('data/descriptors.tsv', 'data/recipes.txt')
    with tempfile.TemporaryDirectory(prefix='search-compat-', dir=out) as directory:
        sparse = Path(directory)
        originals = {name: (root / name).read_bytes() for name in (fixture, *inputs)}
        for name, data in originals.items():
            target = sparse / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        def refused(name, expected_type, message):
            try:
                # Invalid identities must fail before reaching a search method.
                legacy_search_checks(None, sparse, check)
            except (FileNotFoundError, AssertionError) as error:
                check(name, type(error) is expected_type and message in str(error))
            else:
                check(name, False)

        path = sparse / fixture
        path.unlink()
        refused('legacy-search-fixture-missing', FileNotFoundError, 'search_compat.json')
        path.write_bytes(originals[fixture] + b'\n')
        refused('legacy-search-fixture-changed', AssertionError, 'fixture identity differs')
        path.write_bytes(originals[fixture])
        for name in inputs:
            path = sparse / name
            path.write_bytes(originals[name] + b'\n')
            refused('legacy-search-input-' + path.stem, AssertionError,
                    'fixture input differs: ' + name)
            path.write_bytes(originals[name])


def pagination_checks(fm, catalog, analysis, root, harness, out, check, table_path):
    # Compute the recipe ordering/distance independently from catalog.ranked.
    source_rows = analysis.load_descriptor_tables([analysis.DESC_TSV])['rows']
    rows = [(float(row['brightness_hz']), row) for row in source_rows if row['brightness_hz'] != '-']
    scale = max(value for value, _ in rows) - min(value for value, _ in rows) or 1.0
    expected = sorted(rows, key=lambda pair: (abs(pair[0] - 2000) / scale,
                      pair[1]['source'], pair[1]['record'], pair[1]['patch'], pair[1]['name']))
    for size in (7, 19):
        received, offset = [], 0
        while True:
            result = catalog.suggest({'brightness_hz': 2000}, tsv=[analysis.DESC_TSV],
                                     offset=offset, limit=size, api=fm)
            check('rank-page-' + str(size) + '-' + str(offset), result['ok'] and
                  result['page']['total'] == len(expected) and len(result['nearest']) <= size and
                  result['scales']['brightness_hz'] == scale, result)
            received.extend(result['nearest'])
            offset = result['page']['next_offset']
            if offset is None:
                break
        check('rank-page-independent-' + str(size),
              [(r['name'], r['distance']) for r in received] ==
              [(r['name'], abs(v - 2000) / scale) for v, r in expected])
    ties, offset = [], 0
    while True:
        result = catalog.suggest({'brightness_hz': (0, 100000)}, tsv=[analysis.DESC_TSV],
                                 offset=offset, limit=11, api=fm)
        ties.extend(result['zero_distance_ties'])
        check('all-ties-page-' + str(offset), result['zero_distance_ties_page']['total'] == len(rows))
        offset = result['zero_distance_ties_page']['next_offset']
        if offset is None:
            break
    check('all-ties-preserved', len(ties) == len(rows) and len({tuple(t) for t in ties}) == len(rows))
    paths = [analysis.DESC_TSV, table_path]
    expected_search = analysis.search_descriptors(paths, ['brightness_hz=0..'])
    collected, offset = [], 0
    while True:
        result = catalog.search(['brightness_hz=0..'], tsv=paths, offset=offset, limit=9, api=fm)
        check('search-page-' + str(offset), result['ok'] and len(result['matches']) <= 9 and
              result['count'] == len(expected_search['hits']))
        collected.extend(result['matches'])
        offset = result['page']['next_offset']
        if offset is None:
            break
    check('search-pages-exact',
          [{k: row[k] for k in expected_search['columns']} for row in collected] ==
          [{k: row[k] for k in expected_search['columns']} for row in expected_search['hits']])
    check('search-pages-mixed', {'recipes', 'golden'} <= {row['source'] for row in collected})
    for option, value in [('offset', True), ('offset', -1), ('offset', 0.0),
                          ('limit', True), ('limit', 0), ('limit', 101), ('limit', '20')]:
        for operation, args in ((catalog.search, ([],)), (catalog.suggest, ({'brightness_hz': 2000},))):
            result = operation(*args, tsv=[analysis.DESC_TSV], api=fm, **{option: value})
            check('page-refusal-' + operation.__name__ + '-' + option + '-' + repr(value),
                  not result['ok'] and result['exit'] == 2)
    # Follow a discovered catalog path from an unrelated server working directory.
    remote_cwd = out / 'unrelated-cwd'; remote_cwd.mkdir(exist_ok=True)
    selected = next(row for row in collected if row['source'] == 'golden')
    command = [sys.executable, '-B', str(root / 'tools/fm.py'), '--harness', str(harness),
               'describe', selected['record_path'], '--json']
    result = subprocess.run(command, cwd=remote_cwd, capture_output=True, text=True)
    check('catalog-path-nonrepo-cwd', result.returncode == 0 and json.loads(result.stdout)['match'], result.stderr)
    if catalog.CATALOG.is_file():
        result = catalog.suggest({'brightness_hz': 2000}, api=fm)
        check('default-catalog-bounded', len(result['nearest']) == 20 and result['page']['total'] > 20 and
              len(json.dumps(result).encode()) < 200000)
        known = next(row for row in catalog.table_rows(analysis) if row['source'] == 'golden')
        check('bundled-catalog-path', Path(known['record_path']).is_absolute() and
              Path(known['record_path']) == root / known['record'])


def catalog_input_checks(fm, catalog, analysis, root, out, check):
    """Unknown catalog bases never gain caller-cwd identity or raw exceptions."""
    directory = out / 'catalog-inputs'; directory.mkdir(exist_ok=True)
    def table(name, metadata, records):
        path = directory / (name + '.tsv')
        rows = []
        for record, patch in records:
            row = {key: '1' for key in analysis.CATALOG_COLUMNS}
            row.update(name=record + '#' + patch, tags='-', source='piece', record=record,
                       patch=patch, note_hz='440')
            rows.append('\t'.join(row[key] for key in analysis.CATALOG_COLUMNS))
        path.write_text('# catalog ' + json.dumps(metadata) + '\n' + '\t'.join(analysis.CATALOG_COLUMNS)
                        + '\n' + '\n'.join(rows) + '\n')
        return path
    for label, metadata in (('list', ['bad']), ('empty-list', []), ('string', 'bad'), ('number', 1)):
        path = table('bad-' + label, metadata, [('b.tl', 'p')])
        for operation, arguments in ((catalog.search, ([],)), (catalog.suggest, ({'brightness_hz': 1},))):
            name = 'catalog-provenance-' + operation.__name__ + '-' + label
            try:
                reply = operation(*arguments, tsv=[path], api=fm)
            except Exception as error:
                check(name, False, dict(uncaught=type(error).__name__, message=str(error)))
                continue
            check(name, reply.get('ok') is False and reply.get('exit') == 2, reply)
        for suffix, arguments, options in (('no-hits', ['brightness_hz=2..3'], {}),
                                            ('past-page', [], {'offset': 10})):
            reply = catalog.search(arguments, tsv=[path], api=fm, **options)
            check('catalog-provenance-' + label + '-' + suffix,
                  reply.get('ok') is False and reply.get('exit') == 2, reply)
    legacy = table('legacy', {}, [('a.tl', 'p'), ('b.tl', 'p')])
    ambiguous = table('legacy-multi', {}, [('b.tl', 'p'), ('b.tl', 'q')])
    cwd = directory / 'cwd'; cwd.mkdir(exist_ok=True)
    source = cwd / 'b.tl'; source.write_text('identity-only fixture; no render\n')
    previous = Path.cwd()
    try:
        os.chdir(cwd)
        result = catalog.suggest({'brightness_hz': 1}, tsv=[legacy], source=source, api=fm)
        check('legacy-unknown-base-no-cwd-tie', result['ok'] and
              [row['record'] for row in result['nearest']] == ['a.tl', 'b.tl'] and
              all(row['record_path'] is None for row in result['nearest']), result)
        result = catalog.suggest({'brightness_hz': 1}, tsv=[ambiguous], source=source, api=fm)
        check('legacy-unknown-base-no-false-ambiguity', result['ok'] and result['page']['total'] == 2, result)
        absolute = table('absolute', {'record_base': 'absolute'}, [(str(cwd / 'a.tl'), 'p'), (str(source), 'p')])
        result = catalog.suggest({'brightness_hz': 1}, tsv=[absolute], source='b.tl', api=fm)
        check('known-base-explicit-relative-source', result['ok'] and
              result['nearest'][0]['record_path'] == str(source), result)
        log_row = dict(source='take', record='b.tl', record_path=str(source), patch='p', name='take-1',
                       record_sha256='a' * 64)
        check('log-path-and-sha-identity', catalog.preference(log_row, 'b.tl', None) == 0 and
              catalog.preference(log_row, 'a' * 64, None) == 0)
    finally:
        os.chdir(previous)


def directory_checks(fm, harness, root, out, check):
    native = fm.Native(harness)
    inline = root / 'music/recipe-clap-white-44100'
    hashed = fm.resolve_source(inline, native)
    check('directory-inline-hashed', hashed['exit'] == 0 and hashed['match'] is True and
          hashed['absent_sibling_streams'] == ['note'] and len(hashed['source_files']) == 1)
    unpinned = out / 'directory-inline-unpinned'
    unpinned.mkdir(exist_ok=True)
    (unpinned / 'session.tl').write_bytes(fm.edit_canonical(hashed, []))
    unchecked = fm.resolve_source(unpinned, native)
    check('directory-inline-unpinned', unchecked['exit'] == 0 and unchecked['match'] is None and
          unchecked['expected'] is None and unchecked['hash'] == hashed['hash'] and
          unchecked['absent_sibling_streams'] == ['note'])
    accepted = fm.resolve_source(root / 'tests/golden/48000/sine.tl', native)
    events = {row['line_index'] for row in accepted['_rows'] if row['section'] == 'event'}
    rows = accepted['canonical'].splitlines()
    split = out / 'directory-split'; split.mkdir(exist_ok=True)
    (split / 'session.tl').write_text('\n'.join(row for i, row in enumerate(rows) if i not in events) + '\n')
    stream = next(row['raw']['NAME'] for row in accepted['_rows'] if row['id'] == 'header_streams')
    stream = stream[0] if isinstance(stream, list) else stream
    sibling = split / (stream + '.tl')
    sibling.write_text('\n'.join(row for i, row in enumerate(rows) if i in events) + '\n')
    complete = fm.resolve_source(split, native)
    check('directory-split-complete', complete['exit'] == 0 and complete['match'] is True and
          complete['record_sha256'] == accepted['record_sha256'] and
          complete['absent_sibling_streams'] == [] and len(complete['source_files']) == 2)
    sibling.unlink()
    absent = fm.resolve_source(split, native)
    check('directory-split-missing', absent['exit'] == 3 and absent['match'] is False and
          absent['absent_sibling_streams'] == [stream] and len(absent['source_files']) == 1)
    return native


def probe_plan_checks(fm, catalog, root, harness, out, check, measured=False):
    bell = root / 'tests/golden/48000/bell.tl'
    original = bell.read_bytes()
    directory = out / 'plan-directory'; directory.mkdir(exist_ok=True)
    (directory / 'session.tl').write_bytes(original)
    calls = []
    native_run, measure, retain, sweep = fm.Native.run, fm.measure_patch, fm.retain, catalog.probe_sweep
    def counted(native, arguments):
        calls.append(str(arguments[0]))
        return native_run(native, arguments)
    def forbidden(*args, **kwargs):
        raise AssertionError('probe planning performed measurement, candidate or publication work')
    plans = {}
    fm.Native.run, fm.measure_patch, fm.retain, catalog.probe_sweep = counted, forbidden, forbidden, forbidden
    try:
        for name, source, expected_calls in (('file', bell, 1), ('recipe', 'bell', 1),
                                             ('directory', directory, 2)):
            calls.clear()
            plan = catalog.probe_plan(source, harness=harness, api=fm)
            check('plan-' + name, plan['ok'] and plan['exit'] == 0, plan)
            check('plan-native-work-' + name, calls == ['--params', '--gen-params'] + ['--play'] * expected_calls
                  and plan['render_attempts'] == expected_calls, calls)
            check('plan-bound-' + name, fm.reply_bytes(plan) <= 15360 and
                  not plan['measurement_performed'] and not plan['work_reused'])
            check('plan-corpus-scope-' + name, plan['suggest_scope'] == 'descriptor_tables_only; among=None')
            check('plan-candidate-accounting-' + name,
                  plan['candidate_count'] + plan['scalar_skip_count'] ==
                  2 * (plan['field_count'] - plan['unprobeable_field_count']))
            plans[name] = plan
        check('plan-directory-record-identity', plans['file']['source_record_sha256'] ==
              plans['directory']['source_record_sha256'] and plans['file']['source_hash'] ==
              plans['directory']['source_hash'])
        check('plan-directory-cost-delta', all(plans['directory']['costs'][key] == value + 1
              for key, value in plans['file']['costs'].items()))
        check('plan-effective-note', plans['file']['note_millihz'] == 523250 and
              plans['file']['note_source'] == 'first_on')
        parent = catalog.probe_plan(bell, note=440000, parent_record='a' * 64, harness=harness, api=fm)
        check('plan-parent-cost-only', parent['ok'] and parent['parent_record'] == 'a' * 64
              and parent['parent_verified'] is False and parent['note_millihz'] == 440000
              and parent['note_source'] == 'explicit')
        for key, value in plans['file']['costs'].items():
            check('plan-parent-' + key, parent['costs'][key] == value + int(key.startswith('seek_')))
        for index, arguments in enumerate((dict(source=None), dict(source=''), dict(patch=42), dict(patch=''),
                          *[dict(note=value) for value in (True, 0, -1, 440000.0, '440000')],
                          *[dict(parent_record=value) for value in (True, 'a', 'A' * 64, 'a' * 64 + '\n')])):
            calls.clear()
            reply = catalog.probe_plan(harness=harness, api=fm, **dict(dict(source=bell), **arguments))
            check('plan-preflight-' + str(index), reply['exit'] == 2 and not reply['ok']
                  and reply['render_attempts'] == 0 and calls == [], reply)
        bad = out / 'plan-invalid.tl'; bad.write_text('timeline invalid\n')
        stale = out / 'plan-stale.tl'
        stale.write_bytes(original.replace(b'hash 27dd1f8b6cdfcb8e', b'hash 0000000000000000'))
        for source, status in ((bad, 1), (stale, 3), (out / 'plan-missing.tl', 5)):
            calls.clear()
            reply = catalog.probe_plan(source, harness=harness, api=fm)
            check('plan-source-refusal-' + str(status), not reply['ok'] and reply['exit'] == status
                  and reply['render_attempts'] == 1 and calls.count('--play') == 1, reply)
        lines = original.decode().splitlines()
        patches = [line.replace('patch bell ', 'patch alternate ') for line in lines if line.startswith('patch ')]
        multiple = out / 'plan-multiple.tl'
        insertion = next(index for index, line in enumerate(lines) if line.startswith('voice '))
        multiple.write_text('\n'.join(line for line in lines[:insertion] + patches + lines[insertion:]
                                      if not line.startswith('hash ')) + '\n')
        reply = catalog.probe_plan(multiple, harness=harness, api=fm)
        check('plan-ambiguous-patch', reply['exit'] == 2 and reply['render_attempts'] == 1, reply)
        selected = catalog.probe_plan(multiple, patch='alternate', harness=harness, api=fm)
        check('plan-unused-patch-fallback', selected['ok'] and selected['patch'] == 'alternate'
              and selected['note_source'] == 'A4_fallback' and selected['note_millihz'] == 440000, selected)
        missing = catalog.probe_plan(bell, patch='absent', harness=harness, api=fm)
        check('plan-missing-patch', missing['exit'] == 2 and missing['render_attempts'] == 1, missing)
    finally:
        fm.Native.run, fm.measure_patch, fm.retain, catalog.probe_sweep = native_run, measure, retain, sweep
    check('plan-sources-unchanged', bell.read_bytes() == original and
          (directory / 'session.tl').read_bytes() == original and not (directory / 'log.tsv').exists()
          and not list(directory.glob('*.wav')))
    if measured:
        probe = catalog.suggest({'brightness_hz': (1800, 2000)}, source=bell, probe=True,
                                harness=harness, tsv=[fm.analysis_module().DESC_TSV], api=fm)
        check('plan-measured-probe', probe['ok'] and len(probe['moves']) == plans['file']['candidate_count']
              and len(probe['skipped']) == plans['file']['scalar_skip_count'] + plans['file']['unprobeable_field_count']
              and probe['render_attempts'] <= plans['file']['costs']['suggest_probe_renders_upper'], probe)
        plans['measured_probe'] = dict(render_attempts=probe['render_attempts'], candidate_count=len(probe['moves']))
    return plans


def distribution_checks(fm, catalog, analysis, root, harness, out, check):
    """Installed source trees keep recipes and explicit user catalogs usable."""
    # Nest under a real checkout to catch accidental inheritance of its HEAD.
    (root / 'out').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='catalog-package-', dir=root / 'out') as directory:
        package = Path(directory)
        for name in catalog.SOURCES:
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
        optional = package / 'data/catalog.tsv'
        optional.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(catalog, 'ROOT', package), patch.object(catalog, 'CATALOG', optional):
            for name, call in [('search', lambda **kw: catalog.search(tags=['keys'], api=fm, **kw)),
                               ('suggest', lambda **kw: catalog.suggest({'brightness_hz': 1000}, api=fm, **kw))]:
                reply = call()
                rows = reply.get('matches', reply.get('nearest', []))
                check('package-recipes-' + name, reply['ok'] and rows and
                      all(row.get('record_path') is None for row in rows), reply)
                reply = call(tsv=[optional])
                check('explicit-missing-table-' + name, not reply['ok'] and reply['exit'] == 5, reply)
                optional.write_text('invalid descriptor columns\n')
                reply = call()
                check('present-malformed-table-' + name, not reply['ok'] and reply['exit'] == 2, reply)
                optional.unlink()
                optional.mkdir()
                reply = call()
                check('present-directory-table-' + name, not reply['ok'] and reply['exit'] == 5, reply)
                optional.rmdir()
            options = dict(records=[root / 'tests/golden/48000/sine.tl'], harness=harness, api=fm)
            # Native execution uses its absolute path; an installed package must
            # not require a Git executable or use the consumer's enclosing repo.
            with patch.dict(os.environ, {'PATH': ''}):
                reply = catalog.catalog_build(out=package / 'personal.tsv', **options)
            check('package-build-without-git', reply['ok'] and reply['provenance']['source_head'] is None
                  and reply['provenance']['source_dirty'] is None and
                  reply['provenance']['source_sha256'] ==
                  {name: fm.sha((package / name).read_bytes()) for name in catalog.SOURCES}, reply)
            personal = catalog.search(tsv=[package / 'personal.tsv'], api=fm)
            check('package-personal-catalog', personal['ok'] and len(personal['matches']) == 1 and
                  personal['matches'][0]['record_path'] == str(options['records'][0]), personal)
            # Git ignores an empty .git and searches upwards. That must refuse,
            # rather than reporting an unrelated consumer checkout's commit.
            (package / '.git').mkdir()
            refused_path = package / 'broken.tsv'
            refused = catalog.catalog_build(out=refused_path, **options)
            check('package-invalid-local-git', not refused['ok'] and refused['exit'] == 5
                  and not refused_path.exists(), refused)
            (package / '.git').rmdir()
            with patch.object(catalog, 'ROOT', root):
                checkout = catalog.catalog_build(out=package / 'checkout.tsv', **options)
            check('checkout-provenance-preserved', checkout['ok'] and
                  checkout['provenance']['source_head'] == subprocess.check_output(
                      ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip() and
                  isinstance(checkout['provenance']['source_dirty'], bool), checkout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--harness', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--all-goldens', action='store_true', help='Linux acceptance adds all108 golden records')
    parser.add_argument('--directory-only', action='store_true', help='run the four directory input-pool controls')
    parser.add_argument('--plan-only', action='store_true', help='run probe-cost planning and one measured probe control')
    parser.add_argument('--distribution-only', action='store_true', help='run recipe and user-catalog source-export controls')
    args = parser.parse_args()
    root, out = args.repo.resolve(), args.out.resolve()
    try:
        out.relative_to(root)
    except ValueError:
        pass
    else:
        parser.error('--out must be outside the repository')
    out.mkdir(parents=True, exist_ok=True)
    fm = module('regression_fm', root / 'tools/fm.py')
    catalog = module('regression_catalog', root / 'tools/catalog.py')
    analysis = fm.analysis_module()
    harness = args.harness.resolve()
    checks, receipts = [], {}
    def check(name, condition, detail=None):
        if not condition:
            raise AssertionError((name, detail))
        checks.append(name)
    def successful(name, result):
        check(name, result.get('ok') is True and result.get('exit') == 0, result)
        return result
    def source(name, rate=48000):
        return root / 'tests/golden' / str(rate) / (name + '.tl')
    if args.distribution_only:
        distribution_checks(fm, catalog, analysis, root, harness, out, check)
        print('DISTRIBUTIONCHECK ok checks=%d' % len(checks))
        return
    if args.plan_only:
        plans = probe_plan_checks(fm, catalog, root, harness, out, check, measured=True)
        report = dict(version=1, ok=True, checks=len(checks), names=checks, plans=plans,
                      native_sha256=fm.sha(harness.read_bytes()),
                      source_sha256={name: fm.sha((root / name).read_bytes()) for name in
                                     ('tools/catalog.py', 'tools/fm.py', 'tests/catalog/check.py')})
        encoded = json.dumps(report, indent=2, sort_keys=True).replace(str(root), '<repo>').replace(str(out), '<out>')
        (out / 'results.json').write_text(encoded + '\n')
        print('PROBEPLAN ok checks=%d' % len(checks))
        return
    directory_native = directory_checks(fm, harness, root, out, check)
    if args.directory_only:
        report = dict(version=1, ok=True, checks=len(checks), names=checks,
                      native_sha256=directory_native.identity['sha256'],
                      source_sha256={name: fm.sha((root / name).read_bytes()) for name in
                                     ('tools/fm.py', 'tests/catalog/check.py')})
        (out / 'results.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        print('DIRECTORYCHECK ok checks=%d' % len(checks))
        return
    distribution_checks(fm, catalog, analysis, root, harness, out, check)
    probe_plan_checks(fm, catalog, root, harness, out, check)
    catalog_input_checks(fm, catalog, analysis, root, out, check)
    # Recipe identity and numerical duplicates: exact source preference is
    # tested separately from the stable tie order without a preference.
    tables = catalog.table_rows(analysis, [analysis.DESC_TSV])
    for row in tables:
        target = {key: value for key, value in row['descriptors'].items() if value is not None}
        reply = successful('recipe-' + row['name'], catalog.suggest(target, source=row['name'], tsv=[analysis.DESC_TSV], api=fm))
        check('identity-' + row['name'], reply['nearest'][0]['name'] == row['name'] and reply['nearest'][0]['distance'] == 0)
        check('no-probe-' + row['name'], reply['moves'] is None and reply['render_attempts'] == 0)
    for names in [('warm-pad', 'warm-pad-x'), ('hat', 'hat-fixed')]:
        row = next(row for row in tables if row['name'] == names[0])
        reply = catalog.suggest({key: value for key, value in row['descriptors'].items() if value is not None}, tsv=[analysis.DESC_TSV], api=fm)
        ties = [row['name'] for row in reply['nearest'] if row['distance'] == 0]
        check('zero-ties-' + names[0], all(name in ties for name in names) and ties == sorted(ties), ties)
    # Independent distance examples cover inside-range zeros and degenerate
    # column scales; nonfinite target values cannot become successful matches.
    synthetic = [dict(name='a', descriptors={'x': 5., 'y': 8.}), dict(name='b', descriptors={'x': 5., 'y': 12.}),
                 dict(name='missing', descriptors={'x': None, 'y': 10.}), dict(name='nan', descriptors={'x': float('nan'), 'y': 10.})]
    target = {'x': (6., 7.), 'y': (9., 11.)}
    scales = catalog.column_scales(synthetic, target)
    nearest, excluded = catalog.ranked(synthetic, target, scales)
    check('distance-independent', scales == {'x': 1., 'y': 4.} and nearest[0]['distance'] == (1 + .25 ** 2) ** .5)
    check('missing-explicit', len(excluded) == 2 and all(x['reason'] == 'missing_or_nonfinite_measurement' for x in excluded))
    check('inside-interval', catalog.distance({'x': 6, 'y': 10}, target, scales) == (0., []))
    for target in ({'tempo': 1}, {'brightness_hz': float('nan')}, {'brightness_hz': [2, 1]}, {'brightness_hz': [1]}):
        reply = catalog.suggest(target, api=fm)
        check('target-refusal-' + str(len(checks)), reply['ok'] is False and reply['exit'] == 2, reply)
    # Existing recipe searches keep byte-identical successful display output.
    legacy_search_checks(analysis, root, check)
    legacy_search_refusals(root, out, check)
    # Rebuild a compact native catalog twice; optional Linux acceptance adds
    # all goldens without creating persistent WAVs.
    records = sorted((root / 'tests/golden/48000').glob('*.tl')) if args.all_goldens else [source('bell'), source('sine'), source('phase')]
    table_path = out / 'catalog.tsv'
    first = successful('catalog-build', catalog.catalog_build(out=table_path, records=records, harness=harness, api=fm))
    first_bytes = table_path.read_bytes()
    second = successful('catalog-repeat', catalog.catalog_build(out=out / 'catalog-repeat.tsv', records=records, harness=harness, api=fm))
    check('catalog-repeat-identical', first_bytes == (out / 'catalog-repeat.tsv').read_bytes())
    native = fm.Native(harness)
    expected = sum(len({r['raw']['PATCH'] for r in fm.resolve_source(path, native)['_rows'] if r['section'] == 'patch'}) for path in records)
    check('one-row-per-patch', expected == first['patches'])
    check('catalog-profile', first['provenance']['measurement']['fs'] == 48000 and first['provenance']['native_sha256'] == native.identity['sha256'])
    check('catalog-source-identities', first['provenance']['source_sha256'] == {name: fm.sha((root / name).read_bytes()) for name in catalog.SOURCES} and isinstance(first['provenance']['source_dirty'], bool))
    check('catalog-batches', len(first['batches']) == (len(records) + 15) // 16 and all(batch['free_bytes'] > 0 for batch in first['batches']))
    rate_table = out / 'rate.tsv'
    successful('catalog-rate', catalog.catalog_build(out=rate_table, records=[source('sine', 44100)], harness=harness, api=fm))
    check('catalog-force-48k', analysis.load_descriptor_tables([rate_table])['rows'][0]['_metadata']['measurement']['fs'] == 48000)
    joint = successful('combined-search', catalog.search(['brightness_hz=0..'], tsv=[analysis.DESC_TSV, table_path], api=fm))
    check('combined-columns', joint['columns'][-3:] == ['source', 'record', 'patch'])
    pagination_checks(fm, catalog, analysis, root, harness, out, check, table_path)
    tagged = successful('catalog-tags', catalog.search([], tags=['bell'], tsv=[analysis.DESC_TSV, table_path], api=fm))
    check('catalog-never-tags', all(r['source'] == 'recipes' for r in tagged['matches']))
    # Catalog and recipe input errors retain strict search validation.
    for query in ('brightness_hz=nan..', 'brightness_hz=2..1', 'unknown=1..2'):
        refused = catalog.search([query], api=fm)
        check('search-refusal-' + str(len(checks)), not refused['ok'] and refused['exit'] == 2)
    malformed = out / 'malformed.tsv'
    malformed.write_text(first_bytes.decode().replace('\tbrightness_hz\t', '\tunknown_column\t', 1))
    refused = catalog.search([], tsv=[malformed], api=fm)
    check('table-header-refusal', not refused['ok'] and refused['exit'] == 2)
    # Absolute and repo-relative paths select the same exact row among ties.
    descriptor_rows = catalog.table_rows(analysis, [table_path])
    own = next(row for row in descriptor_rows if row['patch'] == 'bell')
    own_target = {key: value for key, value in own['descriptors'].items() if value is not None}
    absolute = catalog.suggest(own_target, source=source('bell'), tsv=[table_path], api=fm)
    relative = catalog.suggest(own_target, source='tests/golden/48000/bell.tl', tsv=[table_path], api=fm)
    check('path-preference', absolute['nearest'] == relative['nearest'] and absolute['nearest'][0]['patch'] == 'bell')
    # The public CLI registers catalog build and shares these same functions.
    cli = subprocess.run([sys.executable, '-B', str(root / 'tools/fm.py'), '--harness', str(harness),
                          'catalog', 'build', '--record', str(source('bell')), '--out', str(out / 'cli.tsv'), '--json'],
                         cwd=root, capture_output=True, text=True)
    check('catalog-cli', cli.returncode == 0 and json.loads(cli.stdout)['patches'] == 1, cli.stderr)
    # An invalid source cannot replace an already committed catalog artifact.
    bad = out / 'bad.tl'; bad.write_text('timeline unknown\n')
    failed = catalog.catalog_build(out=table_path, records=[bad], harness=harness, api=fm)
    check('catalog-atomic-refusal', failed['exit'] == 1 and not failed['ok'] and table_path.read_bytes() == first_bytes, failed)
    # Undefined analysis retains a catalog row and an explicit reason.
    accepted = fm.resolve_source(source('sine'), native)
    carrier = next(r for r in accepted['_rows'] if r['id'] == 'patch' and r['raw']['OP'] == 0)
    output = next(f for f in carrier['fields'] if f['name'] == 'RATIO_Q16')
    silent = out / 'silent.tl'
    silent.write_bytes(fm.edit_canonical(accepted, [dict(row=carrier['line_index'], token=output['token'], component=output['component'], value=0)]))
    silent_table = out / 'silent.tsv'
    unavailable = successful('catalog-undefined', catalog.catalog_build(out=silent_table, records=[silent], harness=harness, api=fm))
    check('catalog-keeps-undefined', unavailable['patches'] == 1 and unavailable['provenance']['unavailable'])
    check('catalog-undefined-fields', any(row['brightness_hz'] == '-' for row in analysis.load_descriptor_tables([silent_table])['rows']))
    reason_rows = catalog.table_rows(analysis, [silent_table])
    check('row-provenance-bounded', all('unavailable' not in row['provenance'] for row in reason_rows))
    check('row-provenance-reason', reason_rows[0]['unavailable']['record'] == reason_rows[0]['record'] and
          reason_rows[0]['unavailable']['patch'] == reason_rows[0]['patch'])
    # Stale source hashes are not silently replaced by catalog generation.
    stale = out / 'stale.tl'
    stale.write_text(source('sine').read_text().replace('hash ' + accepted['hash'], 'hash ' + '0' * 16, 1))
    rejected = catalog.catalog_build(out=table_path, records=[stale], harness=harness, api=fm)
    check('catalog-stale-refusal', not rejected['ok'] and rejected['exit'] == 3 and table_path.read_bytes() == first_bytes)
    # Probe the same actual native fields twice, including a coupled refusal
    # (KS operator extras) and a fractional-unit raw-quantum fallback (phase).
    probes = {}
    for name in ('bell', 'kspluck', 'phase'):
        repeated = []
        for iteration in range(2):
            reply = successful('probe-' + name + '-' + str(iteration), catalog.suggest({'brightness_hz': (1800, 2000)}, source=source(name), probe=True, harness=harness, tsv=[analysis.DESC_TSV], api=fm))
            repeated.append(reply)
        a, b = repeated
        plan = catalog.probe_plan(source(name), harness=harness, api=fm)
        check('plan-probe-candidates-' + name, plan['ok'] and plan['candidate_count'] == len(a['moves'])
              and plan['scalar_skip_count'] + plan['unprobeable_field_count'] == len(a['skipped'])
              and a['render_attempts'] <= plan['costs']['suggest_probe_renders_upper'], plan)
        check('deterministic-' + name, a['moves'] == b['moves'] and a['skipped'] == b['skipped'])
        check('count-attempts-' + name, a['render_attempts'] == 3 + sum(move['render_attempts'] for move in a['moves']))
        check('native-candidates-' + name, all(move['render_attempts'] >= 1 for move in a['moves']))
        probes[name] = dict(attempts=a['render_attempts'], moves=len(a['moves']), seconds=[x['elapsed_seconds'] for x in repeated],
                            measurable=sum(x['measurable'] for x in a['moves']), refused=sum(not x['accepted'] for x in a['moves']),
                            fallback=sum(x['step_policy'] == 'raw_quantum_fallback' for x in a['moves']),
                            moves_sha256=hashlib.sha256(json.dumps(a['moves'], sort_keys=True).encode()).hexdigest())
        if name == 'bell':
            check('probe-changes-measurement', any(move.get('delta', {}).get('brightness_hz', [0, 0, 0])[2] != 0 for move in a['moves']))
        if name == 'kspluck':
            check('coupled-refusal-preserved', any(not move['accepted'] and move['exit'] == 1 and move['stage'] == 'validation' for move in a['moves']))
        if name == 'phase':
            fields = [x for x in a['moves'] if x['field'] == 'PHASE_U16']
            check('phase-fallback', fields and all(x['requested_step_raw'] == 65536 and x['step_raw'] == 1 and x['step_policy'] == 'raw_quantum_fallback' for x in fields))
        # Full move receipts are normalized at final evidence writing below.
        receipts['probe-' + name] = a
    # Log-backed ranking remeasures only isolated patches and records a stable
    # comparable anchor, excluding different note and sample-rate contexts.
    log = out / 'takes'
    for path in (source('sine'), source('sine', 44100)):
        successful('log-' + str(len(checks)), fm.render([path], harness=harness, log=log))
    accepted = fm.resolve_source(source('sine'), native)
    onset = next(r for r in accepted['_rows'] if r['id'] == 'event_on')
    frequency = next(f for f in onset['fields'] if f['name'] == 'FREQ_MILLIHZ')
    changed = out / 'note.tl'
    changed.write_bytes(fm.edit_canonical(accepted, [dict(row=onset['line_index'], token=frequency['token'], component=frequency['component'], value=330000)]))
    successful('log-new-note', fm.render([changed], harness=harness, log=log))
    reply = successful('among-default', catalog.suggest({'brightness_hz': (0, 2000)}, among=log, harness=harness, api=fm))
    check('among-profile-exclusions', reply['anchor']['take'] == 1 and len(reply['nearest']) == 1 and len(reply['excluded']) == 2 and {'fs', 'note_millihz'} == {field for row in reply['excluded'] for field in row['fields']}, reply)
    reply = successful('among-from', catalog.suggest({'brightness_hz': (0, 2000)}, among=log, source=changed, harness=harness, api=fm))
    check('among-from-anchor', reply['anchor']['profile']['note_millihz'] == 330000 and len(reply['nearest']) == 1 and reply['nearest'][0]['take'] == 3)
    reply = successful('among-explicit-note', catalog.suggest({'brightness_hz': (0, 2000)}, among=log, note=440000, harness=harness, api=fm))
    check('among-explicit-note-context', len(reply['nearest']) == 2 and len(reply['excluded']) == 1 and reply['excluded'][0]['fields'] == ['fs'])
    check('no-whole-mix-ranking', all(row['profile']['kind'] == 'isolated_patch_note' for row in reply['nearest']))
    # Artifact-digest verification alone cannot prove the retained PCM hash.
    # Rebuild one syntactically valid log row with a byte-valid TL artifact
    # whose declared audio hash is wrong; native replay must exclude it.
    stale_log = out / 'stale-takes'
    (stale_log / 'records').mkdir(parents=True, exist_ok=True)
    first_log = dict(fm.read_log(log)[0])
    artifact = (log / 'records' / (first_log['record_sha256'] + '.tl')).read_text()
    artifact = artifact.replace('hash ' + first_log['hash'], 'hash ' + '0' * 16).encode()
    digest = hashlib.sha256(artifact).hexdigest()
    (stale_log / 'records' / (digest + '.tl')).write_bytes(artifact)
    first_log.update(hash='0' * 16, record_sha256=digest)
    with (stale_log / 'log.tsv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fm.LOG_COLUMNS, delimiter='\t', lineterminator='\n')
        writer.writeheader(); writer.writerow(first_log)
    excluded = catalog.suggest({'brightness_hz': (0, 2000)}, among=stale_log, harness=harness, api=fm)
    check('among-stale-take', not excluded['ok'] and excluded['excluded'][0]['reason'] == 'source_hash_mismatch' and
          excluded['excluded'][0]['exit'] == 3, excluded)
    anchor = catalog.suggest({'brightness_hz': (0, 2000)}, among=log, source=stale, harness=harness, api=fm)
    check('among-stale-anchor', not anchor['ok'] and anchor['exit'] == 3, anchor)
    check('no-retained-wavs', not list(out.rglob('*.wav')))
    def normalized(value):
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            return value.replace(str(root), '<repo>').replace(str(out), '<out>')
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalized(item) for item in value]
        return value
    report = dict(version=1, ok=True, checks=len(checks), names=checks, probes=probes,
                  catalog=dict(records=len(records), patches=expected, sha256=hashlib.sha256(first_bytes).hexdigest()),
                  native=native.identity, sources={str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (root / 'tools/catalog.py', root / 'tools/fm.py', root / 'tools/music/analyze.py',
                               root / 'tests/catalog/search_compat.json', Path(__file__).resolve())})
    (out / 'results.json').write_text(json.dumps(normalized(report), indent=2, sort_keys=True) + '\n')
    (out / 'probe-receipts.json').write_text(json.dumps(normalized(receipts), indent=2, sort_keys=True) + '\n')
    print('CATALOGCHECK ok checks=%d recipes=%d records=%d patches=%d probes=%d' % (len(checks), len(tables), len(records), expected, len(probes)))


if __name__ == '__main__':
    main()
