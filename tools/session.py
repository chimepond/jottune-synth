# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Explicit snapshots of retained operation and take stores; never execute audio."""
from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import importlib
import io
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile


DIGEST = re.compile(r'[0-9a-f]{64}\Z')
MEMBER = re.compile(r'(operations[.]tsv|log[.]tsv|receipts/[0-9a-f]{64}[.]json|'
                    r'records/[0-9a-f]{64}[.]tl|sounds/[0-9a-f]{64}[.]wav)\Z')
SCOPE = dict(scope='declared_retained_artifacts', external_inputs_captured=False,
             native_binaries_captured=False, replay_performed=False)


def services(api=None):
    return api if api is not None else importlib.import_module('.fm', __package__) if __package__ else importlib.import_module('fm')


def invoke(api, function):
    store = api.receipts_module()
    @api.public
    def run():
        try:
            return function(store)
        except store.ReceiptError as error:
            raise api.ToolError(str(error), error.exit, **error.details) from error
        except (UnicodeError, csv.Error) as error:
            raise api.ToolError('corrupt session text: ' + str(error), 5) from error
    return run()


def resolve_directory(api, value):
    api.require(isinstance(value, (str, Path)) and bool(str(value)), 'directory must be a nonempty path', 2)
    return api.workspace_path(value)


def safe_file(api, root, relative, *, directory=False):
    """Reject noncanonical/traversing member names and symlink components."""
    api.require(isinstance(relative, str) and relative and '\\' not in relative and '\0' not in relative
                and all(part not in ('', '.', '..') for part in relative.split('/'))
                and not relative.startswith('/'), 'unsafe session artifact path', 5)
    path = root
    parts = relative.split('/')
    for index, part in enumerate(parts):
        path = path / part
        mode = path.lstat().st_mode
        api.require(not stat.S_ISLNK(mode), 'session artifacts cannot be symlinks', 5)
        need_directory = directory or index < len(parts) - 1
        api.require(stat.S_ISDIR(mode) if need_directory else stat.S_ISREG(mode),
                    'session artifact has an invalid file type', 5)
    return path


def file_info(api, root, relative):
    path = safe_file(api, root, relative)
    digest, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while True:
            data = stream.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
            size += len(data)
    return dict(bytes=size, sha256=digest.hexdigest())


def root_inventory(api, store, root, kinds):
    """Existing validators remain authoritative; this only closes their file set."""
    files, counts = {}, dict(operations=0, pending_operations=0, takes=0)
    def add(relative, identity=None):
        info = file_info(api, root, relative)
        api.require(identity is None or info['sha256'] == identity, 'session artifact digest mismatch', 5)
        files[relative] = info
    if kinds['operations']:
        add('operations.tsv')
        # Guard referenced paths before the shared parser opens them. The shared
        # parser still checks canonical TSV/JSON, versions, links and outcomes.
        data = safe_file(api, root, 'operations.tsv').read_text(encoding='utf-8')
        csv.field_size_limit(sys.maxsize)
        for row in csv.DictReader(io.StringIO(data), delimiter='\t'):
            for field in ('request_sha256', 'response_sha256'):
                identity = row.get(field)
                if field == 'response_sha256' and identity == '-':
                    continue
                api.require(isinstance(identity, str) and DIGEST.fullmatch(identity), 'invalid receipt artifact identity', 5)
                add('receipts/' + identity + '.json', identity)
                if field == 'response_sha256':
                    response = store._document(root, identity)
                    full = response.get('full_result_sha256') if isinstance(response, dict) else None
                    if full is not None:
                        api.require(isinstance(full, str) and DIGEST.fullmatch(full), 'invalid full result identity', 5)
                        add('receipts/' + full + '.json', full)
        rows, _, verified = store._read(root)
        api.require(hashlib.sha256(verified).hexdigest() == files['operations.tsv']['sha256'],
                    'operation index changed during session snapshot', 5)
        counts.update(operations=len(rows), pending_operations=sum(row['response_sha256'] == '-' for row in rows))
    if kinds['takes']:
        add('log.tsv')
        rows = api.read_log(root, verify=False)
        for row in rows:
            add('records/' + row['record_sha256'] + '.tl', row['record_sha256'])
            if row['wav_sha256'] != '-':
                add('sounds/' + row['wav_sha256'] + '.wav', row['wav_sha256'])
        api.require(file_info(api, root, 'log.tsv') == files['log.tsv'], 'take index changed during session snapshot', 5)
        counts['takes'] = len(rows)
    return files, counts


def summary(api, root, identity, manifest, counts, operation):
    result = dict(version=1, ok=True, exit=0, operation=operation, directory=str(root),
                  manifest_sha256=identity, root_count=len(manifest['roots']), file_count=len(manifest['files']),
                  total_bytes=sum(item['bytes'] for item in manifest['files']), **counts, **SCOPE,
                  artifact=dict(directory=str(root), kind='session_manifest', digest=identity))
    api.require(api.reply_bytes(result) <= 15360, 'session summary exceeds response byte limit', 2)
    return result


def verify(api, store, root, expected=None):
    api.require(expected is None or isinstance(expected, str) and DIGEST.fullmatch(expected),
                'expected_manifest_sha256 must be a lowercase SHA256', 2)
    raw = safe_file(api, root, 'manifest.json').read_bytes()
    identity = hashlib.sha256(raw).hexdigest()
    api.require(expected is None or expected == identity, 'session manifest SHA256 differs', 5)
    manifest = store._json(raw)
    api.require(isinstance(manifest, dict) and set(manifest) == {'version', 'kind', 'roots', 'files', 'scope'}
                and type(manifest['version']) is int and manifest['version'] == 1
                and manifest['kind'] == 'retained_session_bundle' and manifest['scope'] == SCOPE
                and all(type(manifest['scope'][key]) is type(value) for key, value in SCOPE.items()),
                'unsupported session manifest', 5)
    roots, entries = manifest['roots'], manifest['files']
    api.require(isinstance(roots, list) and 1 <= len(roots) <= 32 and isinstance(entries, list),
                'invalid session inventory', 5)
    originals, declared, root_paths = set(), {}, []
    for index, item in enumerate(roots, 1):
        name = 'root-%04d' % index
        api.require(isinstance(item, dict) and set(item) == {'id', 'original', 'path', 'operations', 'takes'}
                    and item['id'] == name and item['path'] == 'roots/' + name
                    and isinstance(item['original'], str) and Path(item['original']).is_absolute()
                    and item['original'] not in originals
                    and type(item['operations']) is bool and type(item['takes']) is bool
                    and (item['operations'] or item['takes']), 'invalid session root mapping', 5)
        originals.add(item['original'])
        root_paths.append(item['path'])
    for item in entries:
        api.require(isinstance(item, dict) and set(item) == {'path', 'bytes', 'sha256'}
                    and isinstance(item['path'], str) and type(item['bytes']) is int and item['bytes'] >= 0
                    and isinstance(item['sha256'], str) and DIGEST.fullmatch(item['sha256']),
                    'invalid session file entry', 5)
        path = item['path']
        prefix = next((prefix for prefix in root_paths if path.startswith(prefix + '/')), None)
        api.require(prefix is not None and MEMBER.fullmatch(path[len(prefix) + 1:])
                    and path not in declared, 'invalid or duplicate session file path', 5)
        actual = file_info(api, root, path)
        api.require(actual == {key: item[key] for key in ('bytes', 'sha256')}, 'session file bytes differ', 5)
        declared[path] = actual
    api.require(list(declared) == sorted(declared), 'session files are not in canonical path order', 5)
    closure, counts = {}, dict(operations=0, pending_operations=0, takes=0)
    for item in roots:
        mapped_root = safe_file(api, root, item['path'], directory=True)
        files, measured = root_inventory(api, store, mapped_root, item)
        closure.update({item['path'] + '/' + name: value for name, value in files.items()})
        for key, value in measured.items():
            counts[key] += value
    api.require(closure == declared, 'session manifest does not match the declared store closure', 5)
    api.require(safe_file(api, root, 'manifest.json').read_bytes() == raw,
                'session manifest changed during verification', 5)
    return identity, manifest, counts


def session_verify(directory, *, expected_manifest_sha256=None, api=None):
    api = services(api)
    def run(store):
        root = resolve_directory(api, directory)
        identity, manifest, counts = verify(api, store, root, expected_manifest_sha256)
        return summary(api, root, identity, manifest, counts, 'session_verify')
    return invoke(api, run)


@contextlib.contextmanager
def destination_lock(api, destination):
    """A stable sidecar lock coordinates publishers without unlinking a live lock."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    name = '.session-export-' + hashlib.sha256(str(destination).encode()).hexdigest() + '.lock'
    path = destination.parent / name
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a+b') as stream:
        api.require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'session publication lock is not a regular file', 5)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield


def copy_member(api, source, relative, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with safe_file(api, source, relative).open('rb') as incoming, target.open('xb') as outgoing:
        while True:
            data = incoming.read(1024 * 1024)
            if not data:
                break
            outgoing.write(data)
            digest.update(data)
            size += len(data)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    api.require(dict(bytes=size, sha256=digest.hexdigest()) == expected,
                'source artifact changed while copying session', 5)


def session_export(sources, out, *, api=None):
    api = services(api)
    def run(store):
        api.require(isinstance(sources, (list, tuple)) and 1 <= len(sources) <= 32,
                    'sources must contain one to32 explicit store directories', 2)
        roots = [resolve_directory(api, source) for source in sources]
        api.require(len(set(roots)) == len(roots), 'duplicate session source root', 2)
        destination = resolve_directory(api, out)
        api.require(not os.path.lexists(destination), 'session destination already exists', 2)
        kinds = {}
        for root in roots:
            api.require(root.is_dir(), 'session source directory does not exist', 5)
            kinds[root] = dict(operations=os.path.lexists(root / 'operations.tsv'), takes=os.path.lexists(root / 'log.tsv'))
            api.require(any(kinds[root].values()), 'session source has no retained operation or take index', 5)
            for present, name in ((kinds[root]['operations'], 'operations.tsv'), (kinds[root]['takes'], 'log.tsv')):
                if present:
                    safe_file(api, root, name)
            for name in ('.operations.lock', '.lock'):
                if os.path.lexists(root / name):
                    safe_file(api, root, name)
        with contextlib.ExitStack() as locks:
            for root in sorted(roots):
                if kinds[root]['operations']:
                    locks.enter_context(store.locked(root))
                if kinds[root]['takes']:
                    locks.enter_context(api.locked(root, create=False))
            inventories = {root: root_inventory(api, store, root, kinds[root]) for root in roots}
            manifest = dict(version=1, kind='retained_session_bundle', roots=[], files=[], scope=dict(SCOPE))
            counts = dict(operations=0, pending_operations=0, takes=0)
            for index, root in enumerate(roots, 1):
                name = 'root-%04d' % index
                prefix = 'roots/' + name
                manifest['roots'].append(dict(id=name, original=str(root), path=prefix, **kinds[root]))
                files, measured = inventories[root]
                manifest['files'].extend(dict(path=prefix + '/' + relative, **info) for relative, info in files.items())
                for key, value in measured.items():
                    counts[key] += value
            manifest['files'].sort(key=lambda entry: entry['path'])
            encoded = store.canonical_bytes(manifest)
            identity = hashlib.sha256(encoded).hexdigest()
            # Check the final public reply before any output publication.
            result = summary(api, destination, identity, manifest, counts, 'session_export')
            with destination_lock(api, destination):
                api.require(not os.path.lexists(destination), 'session destination already exists', 2)
                staging = Path(tempfile.mkdtemp(prefix='.session-pending-', dir=destination.parent))
                try:
                    for root, item in zip(roots, manifest['roots']):
                        for relative, info in inventories[root][0].items():
                            copy_member(api, root, relative, staging / item['path'] / relative, info)
                    store.atomic(staging / 'manifest.json', encoded)
                    verified, _, _ = verify(api, store, staging, identity)
                    api.require(verified == identity, 'staged session manifest changed', 5)
                    api.require(not os.path.lexists(destination), 'session destination already exists', 2)
                    os.rename(staging, destination)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            return result
    return invoke(api, run)
