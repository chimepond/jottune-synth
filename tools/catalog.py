#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Measured catalog and bounded probes over the shared native tool operations.

This module reads descriptor TSVs, not authored timelines. Canonical records,
field edits, isolated-note measurements and take identities belong to fm.py.
"""
from __future__ import annotations

import importlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'data/catalog.tsv'
SOURCES = ('tools/fm.py', 'tools/catalog.py', 'tools/music/analyze.py',
           'tools/music/compose.py', 'src/host/params.def', 'src/host/harness.c')
NUMERIC = re.compile(r'[ui](8|16|32|64)\Z')
PROFILE_KEYS = ('kind', 'fs', 'note_millihz', 'velocity', 'hold_s', 'tail_s',
                'level_db10', 'level_policy', 'native_sha256')


def services(api=None):
    if api is not None:
        return api
    return importlib.import_module('.fm', __package__) if __package__ else importlib.import_module('fm')


def identity(row):
    return tuple(str(row.get(k, '')) for k in ('source', 'record', 'patch', 'name'))


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def requested_targets(api, analysis, target):
    target = api.targets(target) if isinstance(target, (tuple, list)) else target
    api.require(isinstance(target, dict) and target, 'suggest needs at least one descriptor target', 2)
    result = {}
    for key, interval in target.items():
        api.require(key in analysis.DESC_COLUMNS[3:], 'unknown descriptor target: ' + str(key), 2)
        if isinstance(interval, (tuple, list)):
            api.require(len(interval) == 2, 'target range needs two endpoints', 2)
            low, high = interval
        else:
            low = high = interval
        api.require(low is None or finite(low) is not None, 'target lower bound must be finite', 2)
        api.require(high is None or finite(high) is not None, 'target upper bound must be finite', 2)
        low, high = finite(low), finite(high)
        api.require(low is None or high is None or low <= high, 'target lower bound exceeds upper', 2)
        result[key] = (low, high)
    return result


def column_scales(rows, target):
    scales = {}
    for key in target:
        values = [value for row in rows if (value := finite(row.get('descriptors', row).get(key))) is not None]
        scales[key] = max(values) - min(values) if values else 0
        if not math.isfinite(scales[key]):
            raise ValueError('nonfinite descriptor column span: ' + key)
        if scales[key] == 0:
            scales[key] = 1.0
    return scales


def distance(values, target, scales):
    missing = [key for key in target if finite(values.get(key)) is None]
    if missing:
        return None, missing
    components = []
    for key, (low, high) in target.items():
        value = float(values[key])
        error = low - value if low is not None and value < low else value - high if high is not None and value > high else 0
        scale = scales[key]
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError('nonfinite or nonpositive descriptor scale: ' + key)
        component = error / scale
        if not math.isfinite(component):
            raise ValueError('nonfinite normalized descriptor distance: ' + key)
        components.append(component)
    result = math.hypot(*components)
    if not math.isfinite(result):
        raise ValueError('nonfinite combined descriptor distance')
    return result, []


def same_record(record, source):
    if not record or source is None:
        return False
    selected = Path(source)
    if selected.is_dir():
        selected /= 'session.tl'
    return str(record) == str(source) or Path(record).resolve() == selected.resolve()


def preference(row, source, patch):
    if source is None:
        return 1
    exact = (row.get('source') == 'recipes' and row.get('name') == str(source)) or row.get('record_sha256') == str(source)
    exact = exact or (same_record(row.get('record_path'), source) and
                      (patch is None or row.get('patch') == patch))
    return 0 if exact else 1


def ranked(rows, target, scales, source=None, patch=None):
    nearest, excluded = [], []
    for row in rows:
        values = row.get('descriptors', row)
        score, missing = distance(values, target, scales)
        clean = {k: v for k, v in row.items() if not k.startswith('_')}
        if missing:
            excluded.append(dict(identity=list(identity(row)), reason='missing_or_nonfinite_measurement', fields=missing))
        else:
            nearest.append(dict(clean, distance=score))
    nearest.sort(key=lambda row: (row['distance'], preference(row, source, patch), identity(row)))
    return nearest, excluded


def descriptor_paths(analysis, paths=None):
    if paths:
        return list(paths)
    selected = [analysis.DESC_TSV]
    try:
        CATALOG.lstat()
    except FileNotFoundError:
        pass  # Source-only song catalogs are optional in installed packages.
    else:
        selected.append(str(CATALOG))
    return selected


def table_rows(analysis, paths=None):
    selected = descriptor_paths(analysis, paths)
    table = analysis.load_descriptor_tables(selected)
    rows, provenance = [], {}
    for row in table['rows']:
        resolved = record_path(row)
        metadata = row['_metadata']
        key = id(metadata)
        if key not in provenance:
            common = {k: v for k, v in metadata.items() if k != 'unavailable'} if metadata else None
            unavailable = {(item['record'], item['patch']): item for item in (metadata or {}).get('unavailable', [])}
            provenance[key] = (common, unavailable)
        common, unavailable = provenance[key]
        rows.append(dict(name=row['name'], source=row['source'], record=row['record'], patch=row['patch'],
                         record_path=resolved,
                         note_hz=finite(row['note_hz']), tags=row['tags'],
                         descriptors={key: finite(row[key]) for key in analysis.DESC_COLUMNS[3:]},
                         provenance=common, unavailable=unavailable.get((row['record'], row['patch']))))
    return rows


def catalog_metadata(row):
    metadata = row.get('_metadata')
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError('catalog provenance must be a JSON object')
    return metadata or {}


def record_path(row):
    """Resolve only identities with a known base, never the server's cwd."""
    metadata = catalog_metadata(row)
    if not row.get('record'):
        return None
    path = Path(row['record'])
    if path.is_absolute():
        return str(path)
    if Path(row.get('_table', '')).resolve() == CATALOG.resolve() or metadata.get('record_base') == 'repository':
        return str((ROOT / path).resolve())
    # Older subset catalogs stored external basenames without their base.
    return None


def page_arguments(api, offset, limit):
    api.require(type(offset) is int and offset >= 0, 'offset must be an integer at least0', 2)
    api.require(type(limit) is int and 1 <= limit <= 100, 'limit must be an integer from1 to100', 2)


def page(rows, offset, limit):
    selected = rows[offset:offset + limit]
    return selected, dict(offset=offset, limit=limit, total=len(rows), returned=len(selected),
                          next_offset=offset + len(selected) if offset + len(selected) < len(rows) else None)


def search(ranges=(), *, tags=(), tsv=None, offset=0, limit=20, api=None):
    api = services(api)
    @api.public
    def run():
        page_arguments(api, offset, limit)
        analysis = api.analysis_module()
        paths = descriptor_paths(analysis, tsv)
        try:
            found = analysis.search_descriptors(paths, ranges, tags)
            for row in found['rows']:
                catalog_metadata(row)
            selected, pagination = page(found['hits'], offset, limit)
            matches = [dict({key: row[key] for key in found['columns']}, record_path=record_path(row))
                       for row in selected]
        except (ValueError, UnicodeDecodeError) as error:
            raise api.ToolError(str(error), 2) from error
        return dict(version=1, ok=True, exit=0, columns=list(found['columns']),
                    matches=matches, page=pagination,
                    count=len(found['hits']), total=len(found['rows']))
    return run()


def selected_patch(api, accepted, patch):
    patches = sorted({row['raw']['PATCH'] for row in accepted['_rows'] if row['section'] == 'patch'})
    if patch is None:
        api.require(len(patches) == 1, 'multiple patches: provide --patch', 2)
        return patches[0]
    api.require(patch in patches, 'requested patch is absent: ' + patch, 2)
    return patch


def measured(api, native, accepted, patch, note=None, fs=None):
    result = api.measure_patch(native, accepted, patch, note=note, fs=fs)
    if not result.get('measured'):
        raise api.ToolError('patch measurement unavailable: ' + result.get('reason', 'unknown'),
                            result.get('exit', 1), measurement=result)
    return result


def probe_fields(native, accepted, patch):
    metadata = {p['id']: p for p in native.metadata.params}
    result = []
    for row in accepted['_rows']:
        if row['once'] != 'per_patch_op' or row['raw'].get('PATCH') != patch:
            continue
        for field in row['fields']:
            definition = metadata[field['id']]
            if field['name'] != 'OP' and NUMERIC.fullmatch(definition['type']):
                low, high, value = int(definition['lo']), int(definition['hi']), field['raw']
                requested = max(1, math.ceil(float(definition['scale'])))
                step = requested
                policy = 'human_unit'
                if not any(low <= value + sign * step <= high for sign in (-1, 1)):
                    step, policy = 1, 'raw_quantum_fallback'
                result.append(dict(row=row['line_index'], token=field['token'], component=field['component'],
                                   id=field['id'], statement=row['statement'], patch=patch, operator=row['raw']['OP'],
                                   field=field['name'], value=value, lo=low, hi=high,
                                   requested_step_raw=requested, step_raw=step,
                                   step_human=step / float(definition['scale']), step_policy=policy,
                                   probeable=any(low <= value + sign * step <= high for sign in (-1, 1))))
    return sorted(result, key=lambda field: (field['statement'], field['operator'], field['id'], field['token']))


def probe_plan(source, *, patch=None, note=None, parent_record=None, harness=None, api=None):
    """Count existing probe candidates without measuring or rendering a candidate.

    's bounds describe fresh calls on this source. Native source validation
    still renders, and its work is neither cached nor deducted from a later call.
    """
    api = services(api)
    counter = [0]
    def attempt():
        counter[0] += 1
    @api.public
    def run():
        api.require(isinstance(source, (str, Path)) and bool(str(source)),
                    'probe_plan requires an explicit source', 2)
        api.require(patch is None or isinstance(patch, str) and bool(patch),
                    'patch must be a nonempty string', 2)
        api.require(note is None or type(note) is int and note > 0,
                    'note must be a positive integer in mHz', 2)
        api.require(parent_record is None or isinstance(parent_record, str)
                    and re.fullmatch(r'[0-9a-f]{64}', parent_record),
                    'parent_record must be a lowercase SHA256', 2)
        native = api.Native(harness, budget_hook=attempt)
        accepted = api.resolve_source(source, native)
        if accepted['exit'] != 0:
            raise api.ToolError('source does not match its expected hash', accepted['exit'],
                                hash=accepted.get('hash'), expected=accepted.get('expected'))
        selected = selected_patch(api, accepted, patch)
        fields = probe_fields(native, accepted, selected)
        eligible = [field for field in fields if field['probeable']]
        candidates = sum(field['lo'] <= field['value'] + sign * field['step_raw'] <= field['hi']
                         for field in eligible for sign in (-1, 1))
        voices = {row['raw']['INDEX']: row['raw']['PATCH'] for row in accepted['_rows']
                  if row['id'] == 'header_voice'}
        first = next((row['raw']['FREQ_MILLIHZ'] for row in accepted['_rows']
                      if row['id'] == 'event_on' and voices.get(row['raw']['VOICE']) == selected), None)
        effective_note = note if note is not None else first
        if effective_note is None:
            effective_note = api.analysis_module().DESC_NOTE
        baseline = counter[0] + 2 + int(parent_record is not None)
        return dict(version=1, ok=True, exit=0, operation='probe_plan', native=native.identity,
                    source_record_sha256=accepted['record_sha256'], source_hash=accepted['hash'],
                    patch=selected, note_millihz=effective_note,
                    note_source='explicit' if note is not None else 'first_on' if first is not None else 'A4_fallback',
                    parent_record=parent_record, parent_verified=False,
                    field_count=len(fields), candidate_count=candidates,
                    scalar_skip_count=2 * len(eligible) - candidates,
                    unprobeable_field_count=len(fields) - len(eligible),
                    costs=dict(suggest_probe_renders_upper=counter[0] + 2 + 3 * candidates,
                               seek_baseline_publish_renders_upper=baseline,
                               seek_first_improvement_renders_upper=baseline + 3 * candidates + 4),
                    suggest_scope='descriptor_tables_only; among=None',
                    assumptions=['Same canonical source, selected native, patch and note in a fresh call.',
                                 'The suggest bound excludes retained-take ranking through among.',
                                 'Baseline and candidate measurements use the existing two-render note protocol.',
                                 'First-improvement bound assumes one complete improving sweep and winner verification.',
                                 'Refusals, undefined measurements or an already reached target can stop earlier.'],
                    measurement_performed=False, work_reused=False,
                    prediction_scope='structural render upper bounds; no timing or convergence prediction')
    result = run()
    result['render_attempts'] = counter[0]
    if api.reply_bytes(result) > 15360:
        return dict(version=1, ok=False, exit=2, message='probe plan exceeds response byte limit',
                    render_attempts=counter[0])
    return result


def stable_probe_error(details, label='probe'):
    """Generated scratch names must not make otherwise identical moves differ."""
    replacements = {}
    for refusal in details.get('refusals', []):
        filename = refusal.get('file')
        if filename and Path(filename).name in ('candidate.tl', 'note.tl'):
            replacements[filename] = '<' + label + '>/' + Path(filename).name
    def convert(value):
        if isinstance(value, str):
            for old, new in replacements.items():
                value = value.replace(old, new)
        elif isinstance(value, dict):
            value = {key: convert(item) for key, item in value.items()}
        elif isinstance(value, list):
            value = [convert(item) for item in value]
        return value
    return convert(details)


def probe_sweep(api, native, accepted, patch, target, scales, note, counter, before, baseline_distance):
    """Yield native candidates in stable field order; references freeze per sweep."""
    for field in probe_fields(native, accepted, patch):
        if not field['probeable']:
            yield dict(field, reason='no_outer_bound_neighbour'), None, None, True
            continue
        for sign in (-1, 1):
            value = field['value'] + sign * field['step_raw']
            move = dict(field, proposed=value, direction=sign)
            if not field['lo'] <= value <= field['hi']:
                yield dict(move, reason='outside_scalar_outer_bounds'), None, None, True
                continue
            consumed = counter[0]
            change = dict(row=field['row'], token=field['token'], component=field['component'], value=value)
            candidate, result = None, None
            try:
                data = api.edit_canonical(accepted, [change])
                with tempfile.TemporaryDirectory(prefix='fm-probe-') as directory:
                    path = Path(directory) / 'candidate.tl'
                    path.write_bytes(data)
                    candidate = api.native_render(native, [path])
                result = measured(api, native, candidate, patch, note)
                score, missing = distance(result['values'], target, scales)
                if missing:
                    move.update(accepted=True, measurable=False, reason='missing_or_nonfinite_measurement', missing=missing)
                else:
                    move.update(accepted=True, measurable=True, distance=score, improvement=baseline_distance - score,
                                descriptors=result['values'], profile=result['profile'],
                                delta={key: [before['values'][key], result['values'][key], result['values'][key] - before['values'][key]]
                                       for key in target}, hash=candidate['hash'], record_sha256=candidate['record_sha256'],
                                written_hash=result['written_hash'], measured_hash=result['measured_hash'])
            except api.ToolError as error:
                if error.exit == 5:
                    raise
                move.update(accepted=candidate is not None, measurable=False, exit=error.exit,
                            stage='measurement' if candidate is not None else 'validation',
                            reason=str(error), **stable_probe_error(error.details))
            move['render_attempts'] = counter[0] - consumed
            yield move, candidate, result, False


def run_probe(api, native, accepted, patch, target, scales, note, counter):
    start = time.perf_counter()
    before = measured(api, native, accepted, patch, note)
    baseline_distance, missing = distance(before['values'], target, scales)
    api.require(not missing, 'baseline lacks requested descriptors', 1)
    moves, skipped = [], []
    for move, _, _, is_skipped in probe_sweep(api, native, accepted, patch, target, scales, note,
                                             counter, before, baseline_distance):
        (skipped if is_skipped else moves).append(move)
    moves.sort(key=lambda move: (not move.get('measurable', False), -move.get('improvement', 0),
                                 move['statement'], move['operator'], move['id'], move['direction']))
    return dict(baseline=before, baseline_distance=baseline_distance, moves=moves, skipped=skipped,
                elapsed_seconds=time.perf_counter() - start)


def profile_signature(profile):
    return tuple(profile.get(key) for key in PROFILE_KEYS)


def among_rows(api, native, directory, patch, note, source=None):
    directory = Path(directory)
    logs = api.read_log(directory, verify=True)
    rows, excluded, anchor = [], [], None
    if source is not None:
        accepted = api.resolve_source(source, native)
        if accepted['exit'] != 0:
            raise api.ToolError('source anchor does not match its expected hash', accepted['exit'],
                                hash=accepted.get('hash'), expected=accepted.get('expected'))
        name = selected_patch(api, accepted, patch)
        result = measured(api, native, accepted, name, note)
        anchor = dict(source=str(source), patch=name, record_sha256=accepted['record_sha256'], profile=result['profile'])
    for log in logs:
        path = directory / 'records' / (log['record_sha256'] + '.tl')
        try:
            accepted = api.resolve_source(path, native)
            if accepted['exit'] != 0:
                excluded.append(dict(take=int(log['take']), record_sha256=log['record_sha256'],
                                     reason='source_hash_mismatch', exit=accepted['exit'],
                                     hash=accepted.get('hash'), expected=accepted.get('expected')))
                continue
            name = selected_patch(api, accepted, patch)
            result = measured(api, native, accepted, name, note)
        except api.ToolError as error:
            if error.exit == 5:
                raise
            excluded.append(dict(take=int(log['take']), record_sha256=log['record_sha256'], reason=str(error)))
            continue
        profile = result['profile']
        if anchor is None:
            anchor = dict(take=int(log['take']), record_sha256=log['record_sha256'], patch=name, profile=profile)
        differences = [key for key in PROFILE_KEYS if profile.get(key) != anchor['profile'].get(key)]
        if differences:
            excluded.append(dict(take=int(log['take']), record_sha256=log['record_sha256'],
                                 reason='incompatible_measurement_profile', fields=differences, profile=profile))
            continue
        rows.append(dict(name='take-' + log['take'], source='take', record=str(path), record_path=str(path.resolve()), patch=name,
                         take=int(log['take']), record_sha256=log['record_sha256'], descriptors=result['values'], profile=profile))
    if not rows:
        raise api.ToolError('no comparable measured takes remain', 1, excluded=excluded, anchor=anchor)
    return rows, excluded, anchor


def suggest(target, *, source=None, patch=None, probe=False, among=None, note=None,
            tsv=None, harness=None, offset=0, limit=20, api=None):
    api = services(api)
    @api.public
    def run():
        page_arguments(api, offset, limit)
        api.require(type(probe) is bool, 'probe must be a boolean', 2)
        api.require(note is None or (type(note) is int and note > 0), '--note is a positive integer in mHz', 2)
        analysis = api.analysis_module()
        wanted = requested_targets(api, analysis, target)
        api.require(not probe or source is not None, '--probe requires --from', 2)
        api.require(among is None or not tsv, '--among and --tsv select different candidate corpora', 2)
        counter = [0]
        def attempt():
            counter[0] += 1
        native = api.Native(harness, budget_hook=attempt) if probe or among is not None else None
        excluded, anchor = [], None
        try:
            if among is not None:
                rows, excluded, anchor = among_rows(api, native, among, patch, note, source)
            else:
                rows = table_rows(analysis, tsv)
        except (ValueError, UnicodeDecodeError) as error:
            raise api.ToolError(str(error), 2) from error
        matching = {row['patch'] for row in rows if same_record(row.get('record_path'), source)}
        api.require(patch is not None or len(matching) <= 1, 'multiple patches: provide --patch', 2)
        scales = column_scales(rows, wanted)
        nearest, unavailable = ranked(rows, wanted, scales, source, patch)
        excluded += unavailable
        if among is not None and not nearest:
            raise api.ToolError('no comparable measured takes remain', 1, excluded=excluded, anchor=anchor)
        space = None
        if note is not None:
            api.require(isinstance(note, int) and note > 0, '--note is a positive integer in mHz', 2)
            compose = api.compose_module()
            space = dict(keys_pos=list(compose.keys_pos(compose.midi_of(note))), max_deg_per_s=3)
        selected, pagination = page(nearest, offset, limit)
        excluded_selected, excluded_pagination = page(excluded, offset, limit)
        ties, ties_pagination = page([list(identity(row)) for row in nearest if row['distance'] == 0], offset, limit)
        reply = dict(version=1, ok=True, exit=0, target=wanted, source=str(source) if source is not None else None,
                     nearest=selected, page=pagination,
                     scales=scales, excluded=excluded_selected, excluded_page=excluded_pagination,
                     moves=None, space=space, anchor=anchor,
                     zero_distance_ties=ties, zero_distance_ties_page=ties_pagination)
        if probe:
            accepted = api.resolve_source(source, native)
            name = selected_patch(api, accepted, patch)
            result = run_probe(api, native, accepted, name, wanted, scales, note, counter)
            reply.update(result)
        reply['render_attempts'] = counter[0]
        if native:
            reply['native'] = native.identity
        return reply
    return run()



class BudgetExhausted(Exception):
    """Internal control flow raised before a native call would exceed budget."""


def tolerance_target(api, target, tol):
    if isinstance(tol, (list, tuple)):
        parsed = {}
        for item in tol:
            key, separator, value = str(item).partition('=')
            api.require(separator and key not in parsed, 'tolerance needs unique KEY=T entries', 2)
            parsed[key] = value
        tol = parsed
    api.require(tol is None or isinstance(tol, dict), 'tolerance must map target keys to scalar values', 2)
    supplied = tol or {}
    api.require(all(key in target for key in supplied), 'tolerance names an unknown target key', 2)
    tolerance, expanded = {}, {}
    for key, (low, high) in target.items():
        value = supplied.get(key, 0)
        amount = finite(value)
        api.require(not isinstance(value, bool) and amount is not None and amount >= 0,
                    'tolerance must be finite and nonnegative: ' + key, 2)
        lower = low - amount if low is not None else None
        upper = high + amount if high is not None else None
        api.require((lower is None or math.isfinite(lower)) and (upper is None or math.isfinite(upper)),
                    'expanded tolerance endpoint is nonfinite: ' + key, 2)
        tolerance[key], expanded[key] = amount, (lower, upper)
    return tolerance, expanded


def seek(target, *, source, log, budget, tol=None, patch=None, note=None, tsv=None,
         harness=None, parent_record=None, api=None):
    api = services(api)
    @api.public
    def run():
        api.require(isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0,
                    'budget must be an integer at least zero', 2)
        api.require(log is not None, 'seek requires an explicit log directory', 2)
        api.require(source is not None, 'seek requires --from', 2)
        api.require(note is None or isinstance(note, int) and not isinstance(note, bool) and note > 0,
                    '--note is a positive integer in mHz', 2)
        analysis = api.analysis_module()
        wanted = requested_targets(api, analysis, target)
        tolerance, expanded = tolerance_target(api, wanted, tol)
        scales = column_scales(table_rows(analysis, tsv), wanted)
        counter, phase, counts = [0], ['baseline_source'], {}
        def attempt():
            if counter[0] >= budget:
                raise BudgetExhausted()
            counter[0] += 1
            counts[phase[0]] = counts.get(phase[0], 0) + 1
        native = api.Native(harness, budget_hook=attempt)
        reply = dict(version=1, ok=True, exit=0, native=native.identity, target=wanted,
                     tolerance=tolerance, scales=scales, budget=budget, source=str(source),
                     log=str(Path(log).resolve()), reached=None, stop_reason=None,
                     accepted=None, path=None, measurements=None, history=[], sweeps=[])
        def complete(stop_reason, *, error=None, **details):
            reply.update(stop_reason=stop_reason, consumed_renders=counter[0], render_attempts=counter[0],
                         render_counts=dict(counts), **details)
            if error is not None:
                reply.update(ok=False, exit=getattr(error, 'exit', 1), message=str(error))
                if isinstance(error, api.ToolError):
                    reply['error'] = stable_probe_error(error.details, 'seek')
            return reply
        def publish(accepted, measurement, score, previous, move=None):
            phase[0] = 'verify_parent' if move is not None else 'baseline_parent'
            retained = api.retain(accepted, native, log=log, parent_record=previous,
                                  note_text='seek verified improvement' if move else 'seek baseline')
            view = dict(record=retained['record'], record_sha256=retained['record_sha256'],
                        hash=retained['hash'], take=retained['take'],
                        parent_record_sha256=retained.get('parent_record_sha256'),
                        measurement=measurement, distance=score)
            if move is not None:
                view['move'] = move
            reply.update(accepted=view, path=view['record'], measurements=measurement,
                         reached=None if score is None else distance(measurement['values'], expanded, scales)[0] == 0)
            reply['history'].append(view)
        try:
            accepted = api.resolve_source(source, native)
            if accepted['exit'] != 0:
                raise api.ToolError('source does not match its expected hash', accepted['exit'],
                                    hash=accepted.get('hash'), expected=accepted.get('expected'))
            name = selected_patch(api, accepted, patch)
            reply['patch'] = name
            phase[0] = 'baseline_measurement'
            before = api.measure_patch(native, accepted, name, note=note)
            if not before.get('measured'):
                return complete('undefined_measurement', reason=before.get('reason', 'baseline measurement unavailable'),
                                measurements=before)
            score, missing = distance(before['values'], wanted, scales)
            publish(accepted, before, score, parent_record)
            if missing:
                return complete('undefined_measurement', reason='baseline lacks requested descriptors', missing=missing)
            if reply['reached']:
                return complete('reached')
            while True:
                sweep = dict(iteration=len(reply['sweeps']) + 1, complete=False, candidates=0,
                             refused=0, skipped=0, render_start=counter[0], best=None)
                reply['sweeps'].append(sweep)
                best = None
                phase[0] = 'probe'
                try:
                    for move, candidate, measurement, skipped in probe_sweep(api, native, accepted, name, wanted,
                                                                           scales, note, counter, before, score):
                        if skipped:
                            sweep['skipped'] += 1
                            continue
                        sweep['candidates'] += 1
                        sweep['refused'] += not move['accepted']
                        if move.get('measurable') and move['distance'] < score:
                            key = (move['distance'], move['statement'], move['operator'], move['id'], move['direction'])
                            if best is None or key < best[0]:
                                best = (key, move, candidate, measurement)
                                sweep['best'] = move
                finally:
                    sweep['render_attempts'] = counter[0] - sweep['render_start']
                sweep['complete'] = True
                if best is None:
                    return complete('stagnation')
                _, move, proposed, proposal_measure = best
                phase[0] = 'verify_source'
                with tempfile.TemporaryDirectory(prefix='fm-seek-') as directory:
                    path = Path(directory) / 'candidate.tl'
                    path.write_bytes(proposed['_record_bytes'])
                    verified = api.native_render(native, [path])
                if verified['exit'] != 0 or any(verified[key] != proposed[key] for key in ('record_sha256', 'hash')):
                    return complete('verification_mismatch', error=api.ToolError('independent record verification differs', 1),
                                    verification=dict(proposed_record_sha256=proposed['record_sha256'],
                                                      actual_record_sha256=verified['record_sha256'],
                                                      proposed_hash=proposed['hash'], actual_hash=verified['hash']))
                phase[0] = 'verify_measurement'
                verification_start = counter[0]
                verification = api.measure_patch(native, verified, name, note=note)
                equal = (counter[0] - verification_start == 2 and
                         verification.get('measured') is True and
                         all(verification.get(key) == proposal_measure.get(key)
                             for key in ('written_hash', 'measured_hash', 'written_record_sha256',
                                         'measured_record_sha256', 'profile', 'values')))
                if not equal:
                    return complete('verification_mismatch', error=api.ToolError('independent candidate verification differs', 1),
                                    verification=dict(proposed_record_sha256=proposed['record_sha256'],
                                                      actual_record_sha256=verified['record_sha256'],
                                                      note_render_attempts=counter[0] - verification_start,
                                                      proposed=proposal_measure, actual=verification))
                previous = reply['accepted']['record_sha256']
                publish(verified, verification, move['distance'], previous, move)
                accepted, before, score = verified, verification, move['distance']
                if reply['reached']:
                    return complete('reached')
        except BudgetExhausted:
            return complete('budget')
        except api.ToolError as error:
            return complete('host_error' if error.exit == 5 else 'refused', error=error)
        except (OSError, subprocess.SubprocessError, ImportError) as error:
            return complete('host_error', error=api.ToolError(str(error), 5))
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            return complete('refused', error=error)
    return run()

def catalog_build(*, out, harness=None, records=None, batch_size=16, api=None):
    api = services(api)
    @api.public
    def run():
        api.require(type(batch_size) is int and batch_size > 0, 'batch size must be a positive integer', 2)
        api.require(records is not None or sys.platform.startswith('linux'),
                    'the full catalog is built on Linux; --record selects a focused subset', 2)
        analysis = api.analysis_module()
        source_paths = sorted(map(Path, records)) if records is not None else sorted((ROOT / 'tests/golden/48000').glob('*.tl')) + sorted((ROOT / 'music').glob('**/session.tl'))
        api.require(source_paths, 'catalog needs at least one record', 2)
        attempts = [0]
        def attempt():
            attempts[0] += 1
        native = api.Native(harness, budget_hook=attempt)
        destination = Path(out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rows, batches, unavailable = [], [], []
        for index, path in enumerate(source_paths):
            if index % batch_size == 0:
                usage = shutil.disk_usage(destination.parent)
                batches.append(dict(first_record=index, free_bytes=usage.free, total_bytes=usage.total))
            accepted = api.resolve_source(path.parent if path.name == 'session.tl' else path, native)
            if accepted['exit'] != 0:
                raise api.ToolError('catalog source does not match its expected hash', accepted['exit'],
                                    record=str(path), hash=accepted.get('hash'), expected=accepted.get('expected'))
            if records is not None:
                record = str(path.resolve())
            else:
                record = path.resolve().relative_to(ROOT).as_posix()
            try:
                path.resolve().relative_to(ROOT / 'tests/golden')
                source = 'golden'
            except ValueError:
                source = 'piece'
            patches = sorted({row['raw']['PATCH'] for row in accepted['_rows'] if row['section'] == 'patch'})
            for patch in patches:
                try:
                    result = api.measure_patch(native, accepted, patch, fs=analysis.DESC_FS)
                except api.ToolError as error:
                    if error.exit == 5:
                        raise
                    detail = stable_probe_error(error.details, 'catalog')
                    result = dict(measured=False, reason=str(error), exit=error.exit,
                                  refusals=detail.get('refusals', []))
                numbers = {key: finite((result.get('values') or {}).get(key)) for key in analysis.DESC_COLUMNS[3:]}
                missing = [key for key, value in numbers.items() if value is None]
                if not result.get('measured') or missing:
                    unavailable.append(dict(record=record, patch=patch, fields=missing,
                                            measured=bool(result.get('measured')),
                                            reason=result.get('reason', 'undefined_descriptor'),
                                            exit=result.get('exit'), refusals=result.get('refusals', [])))
                mhz = result.get('profile', {}).get('note_millihz')
                values = dict(numbers, name=record + '#' + patch, tags=[],
                              note_hz=mhz / 1000 if mhz is not None else None,
                              source=source, record=record, patch=patch)
                rows.append(values)
        source_head, source_dirty = None, None
        try:
            (ROOT / '.git').lstat()
        except FileNotFoundError:
            pass  # Do not inherit the installing project's unrelated Git state.
        else:
            top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=ROOT, capture_output=True, text=True)
            api.require(top.returncode == 0 and Path(top.stdout.strip()).resolve() == ROOT.resolve(),
                        'catalog source Git root is unavailable or differs', 5)
            head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
            api.require(head.returncode == 0, 'catalog source HEAD is unavailable', 5)
            dirty = subprocess.run(['git', 'status', '--porcelain', '--', *SOURCES], cwd=ROOT, capture_output=True, text=True)
            api.require(dirty.returncode == 0, 'catalog source status is unavailable', 5)
            source_head, source_dirty = head.stdout.strip(), bool(dirty.stdout.strip())
        source_hashes = {path: api.sha((ROOT / path).read_bytes()) for path in SOURCES}
        provenance = dict(version=1, native_sha256=native.identity['sha256'], engine=native.identity['engine'],
                          record_base='absolute' if records is not None else 'repository',
                          source_head=source_head, source_dirty=source_dirty, source_sha256=source_hashes,
                          records=len(source_paths), patches=len(rows), unavailable=unavailable,
                          measurement=dict(kind='isolated_patch_note', fs=analysis.DESC_FS,
                                           note_policy='first_on_else_A4', velocity=analysis.DESC_VEL,
                                           hold_s=analysis.DESC_HOLD, tail_s=analysis.DESC_TAIL,
                                           level_db10=analysis.DESC_LEVEL, level_policy='D83_carrier_sum'))
        text = '# catalog ' + json.dumps(provenance, sort_keys=True, separators=(',', ':')) + '\n'
        text += '\t'.join(analysis.CATALOG_COLUMNS) + '\n'
        for row in rows:
            text += '\t'.join(analysis.desc_cell(key, row[key]) if key in analysis.DESC_COLUMNS else str(row[key])
                              for key in analysis.CATALOG_COLUMNS) + '\n'
        api.atomic(destination, text.encode('utf-8'))
        return dict(version=1, ok=True, exit=0, out=str(destination), provenance=provenance,
                    records=len(source_paths), patches=len(rows), render_attempts=attempts[0], batches=batches)
    return run()
