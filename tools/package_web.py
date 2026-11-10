#!/usr/bin/env python3
# Copyright (c) 2026 Nenad Micic
# SPDX-License-Identifier: Apache-2.0
"""Package local renderer artifacts and their matching browser host runtime.

Development builds discard unavailable development registry entries. Kept and
withdrawn entries remain: restore their original archives before staging a site.
"""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('harness.wasm', 'browser.wasm', 'library.wasm')
NOTICES = ('wasi-libc-LICENSE', 'compiler-rt-LICENSE')
HOSTED_DOCUMENTS = {
    'docs/INSTRUMENT.md': 'instrument.txt',
    'docs/INTERCHANGE.md': 'interchange.txt',
    'docs/TL-FORMAT.md': 'tl-format.txt',
    'tools/README.md': 'local-tools.txt',
    'README.md': 'project.txt',
    'data/recipes.txt': 'reference/recipes.txt',
    'data/tables.txt': 'reference/tables.txt',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_available(web, entry):
    paths = [entry.get('wasm'), entry.get('browser_wasm')]
    paths.extend(entry[key] for key in ('library_wasm', 'runtime_module') if entry.get(key))
    return all(isinstance(path, str) and (web / path).is_file() for path in paths)


def source_commit(root):
    """Only committed, clean renderer inputs can identify a kept release."""
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal',
            '--', 'src', 'vendor', 'LICENSE', 'Makefile', 'tools/build_wasm.py', 'tools/package_web.py', 'tools/wasm',
            'web/runtime.mjs', 'web/vendor'], cwd=root, text=True)
        return None if dirty else head
    except (OSError, subprocess.CalledProcessError):
        return None


# A kept release names the https URL of its public repository and a commit that a
# remote-tracking branch of that repository already contains. The branch's remote
# must fetch from that repository (repository_key).
REPOSITORY = re.compile(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~-]+)+/?')
PUBLIC_REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]*')


def repository_key(url):
    """The host and path that an https, ssh or scp-like Git URL names, or None.

    The host's letter case, a trailing slash and a final .git do not count, so
    git@host:owner/repo.git and https://host/owner/repo name the same repository."""
    url = url.strip()
    match = (re.fullmatch(r'(?:https|ssh)://(?:[^/@]+@)?([^/:@]+)(?::[0-9]+)?/(.+)', url) or
             re.fullmatch(r'(?:[^/@:]+@)?([^/:@]+):(?!/)(.+)', url))
    if not match:
        return None
    path = match[2].strip('/')
    path = path[:-4] if path.endswith('.git') else path
    return match[1].lower() + '/' + path if path else None


def published_commit(root, revision, ref, repository):
    """True when ref is a remote-tracking branch that contains revision, and the
    remote it is named after (refs/remotes/<remote>/...) fetches from repository.

    Fetch the public repository first. This reads the local repository only: it
    catches packaging from the wrong remote, not edited local refs."""
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True)
    try:
        full = git('rev-parse', '--symbolic-full-name', ref)
        name = full.stdout.strip()
        if full.returncode or not name.startswith('refs/remotes/'):
            return False
        # Remote names may hold a slash; the longest configured name that prefixes the ref wins.
        remotes = [remote for remote in git('remote').stdout.split() if name.startswith('refs/remotes/' + remote + '/')]
        if not remotes:
            return False
        url = git('remote', 'get-url', max(remotes, key=len))
        expected = repository_key(repository)
        if url.returncode or expected is None or repository_key(url.stdout) != expected:
            return False
        return git('merge-base', '--is-ancestor', revision, name).returncode == 0
    except OSError:
        return False


def toolchain_notices(lib, zig_license):
    """Notice texts copied from the exact Zig distribution that built the modules.

    wasi-libc-LICENSE holds wasi-libc's pointer file and then every licence or
    copyright file under libc/wasi, so the BSD-2-Clause cloudlibc licence and the
    musl copyright that the pointer file names travel with every served module.
    A missing pointer or cloudlibc licence, or an empty file, is an error."""
    lib = Path(lib)
    wasi = lib / 'libc/wasi'
    found = sorted(p for p in wasi.rglob('*') if p.is_file()
                   and re.fullmatch(r'(LICENSE|COPYING|COPYRIGHT)(-[A-Z0-9-]+)?', p.name))
    for required in (wasi / 'LICENSE', wasi / 'libc-bottom-half/cloudlibc/LICENSE'):
        if required not in found:
            raise ValueError('Missing toolchain licence: ' + required.relative_to(lib).as_posix())
    inputs = {
        'wasi-libc-LICENSE': [wasi / 'LICENSE'] + [p for p in found if p != wasi / 'LICENSE'],
        'compiler-rt-LICENSE': [Path(zig_license), lib / 'libc/musl/COPYRIGHT', wasi / 'LICENSE-APACHE-LLVM'],
    }
    notices = {}
    for name, paths in inputs.items():
        blocks = []
        for path in paths:
            text = path.read_text()
            if not text.strip():
                raise ValueError('Empty toolchain licence: ' + path.name)
            where = path.relative_to(lib).as_posix() if path.is_relative_to(lib) else 'Zig ' + path.name
            blocks.append(where + '\n\n' + text.rstrip())
        notices[name] = ('\n\n' + '=' * 72 + '\n\n').join(blocks) + '\n'
    return notices


def write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def hosted_text(text, source, available, *, native=False):
    """Rewrite documentation links for a standalone static player.

    Source-only implementation references remain honest citations, without
    suggesting that a source checkout path is a downloadable website asset.
    """
    def link(match):
        label, target = match.group(1), match.group(2)
        target = target[1:-1] if target.startswith('<') and target.endswith('>') else target
        if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target) or target.startswith('//'):
            return match.group(0)
        if target.startswith('#'):
            return f'[{label}](instrument.txt{target})' if native else match.group(0)
        local, separator, fragment = target.partition('#')
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), local))
        destination = HOSTED_DOCUMENTS.get(resolved)
        if resolved in ('music/library.json', 'examples/library.json'):
            destination = 'songs.json'
        elif resolved.startswith('web/'):
            destination = resolved[4:]
        if destination in available:
            return f'[{label}]({destination}{separator}{fragment})'
        return f'{label} (source checkout: `{resolved}{separator}{fragment}`)'
    return re.sub(r'\[([^\]\n]+)\]\((<[^>\n]+>|[^\s)]+)\)', link, text)


def stage_agent_references(root):
    root, web = Path(root), Path(root) / 'web'
    sources = {source:target for source,target in HOSTED_DOCUMENTS.items() if (root / source).is_file()}
    available = {path.relative_to(web).as_posix() for path in web.rglob('*') if path.is_file()}
    available.update(sources.values())
    for source, target in sources.items():
        destination = web / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        text = (root / source).read_text()
        destination.write_text(hosted_text(text, source, available))
        if source == 'docs/INSTRUMENT.md':
            heading = '## Native instrument and timeline reference'
            if heading in text:
                native = text[text.index(heading):]
                (web / 'native-reference.txt').write_text(hosted_text(native, source, available, native=True))
            elif (web / 'native-reference.txt').exists():
                (web / 'native-reference.txt').unlink()
    for source,target in [('LICENSE','LICENSE'), ('NOTICE','NOTICE')]:
        if (root / source).is_file():
            shutil.copyfile(root / source, web / target)


def stage_analysis(root):
    """A separately built optional analyzer never replaces a kept renderer."""
    root, web = Path(root), Path(root) / 'web'
    build = root / 'build/wasm'
    if not any((build / name).exists() for name in ('analysis.wasm','analysis.json')):
        return
    try:
        metadata = json.loads((build / 'analysis.json').read_text())
        if not isinstance(metadata, dict) or metadata.get('format') != 1 or any(
            metadata.get(key) != digest(build / name) for key,name in (
                ('wasm_sha256','analysis.wasm'), ('browser_sha256','browser.wasm'), ('library_sha256','library.wasm'))):
            raise ValueError('Analysis companion does not match its build inputs.')
        registry = json.loads((web / 'engines.json').read_text())
        if not any(entry.get('browser_sha256') == metadata['browser_sha256'] and
                   entry.get('library_sha256') == metadata['library_sha256'] for entry in registry.get('engines', [])):
            raise ValueError('Analysis companion is not bound to an installed renderer.')
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError('Build the optional native analysis companion with make analysis-web.') from error
    shutil.copyfile(build / 'analysis.wasm', web / 'analysis-view.wasm')
    shutil.copyfile(build / 'analysis.json', web / 'analysis-view.json')


def agent_manifest(web):
    """Pin static client dependencies, including every available archived shim.

    Presentation and song collections do not invalidate or inflate the runtime
    package.
    Missing historical artifacts stay missing: clients refuse that release.
    """
    web = Path(web)
    if not (web / 'agent.mjs').is_file() or not (web / 'engines.json').is_file():
        return
    allowed_json = {'engines.json','starter.score.json','analysis-view.json','spatial-view.json'}
    excluded = {'songs','catalog'}
    entries = []
    for file in sorted(web.rglob('*')):
        relative = file.relative_to(web)
        if (any(part.startswith('.') for part in relative.parts) or relative.parts[0] in excluded or
                file.name in ('sw.js','sw.template.js','precache.json') or file.suffix in ('.html','.css')):
            continue
        if file.is_symlink():
            raise ValueError('Static client assets must not be symlinks: ' + relative.as_posix())
        if not file.is_file():
            continue
        archive = relative.parts[0] == 'engines'
        shared = file.suffix in ('.mjs','.js','.txt','.wasm') or relative.parts[0] in ('vendor','reference') or file.name in allowed_json or file.name in ('LICENSE','NOTICE','starter.tl','example.tl')
        if not archive and not shared:
            continue
        name = relative.as_posix()
        if (len(name) > 512 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', name) or file.stat().st_size > 64 * 1024 * 1024):
            raise ValueError('Unsafe or oversized static client asset: ' + name)
        entries.append({'path':name, 'sha256':digest(file), 'bytes':file.stat().st_size})
    if len(entries) > 4096 or len({entry['path'].lower() for entry in entries}) != len(entries):
        raise ValueError('Too many or duplicate static client assets.')
    by_path = {entry['path']:entry for entry in entries}
    for name in ('agent.mjs','engines.json','worker.mjs','runtime.mjs','policy.mjs','media.mjs','links.mjs','tl-document.mjs'):
        if name not in by_path:
            raise ValueError('Static client is missing required asset: ' + name)
    registry = json.loads((web / 'engines.json').read_text())
    for engine in registry.get('engines', []):
        for key,digest_key in [('browser_wasm','browser_sha256'), ('runtime_module','runtime_sha256'),
                               ('library_wasm','library_sha256'), ('wasm','sha256')]:
            asset = by_path.get(engine.get(key))
            if asset and (not asset['path'].startswith('engines/' + str(engine.get('id')) + '/') or
                          asset['sha256'] != engine.get(digest_key)):
                raise ValueError('Installed renderer digest/path differs from registry: ' + asset['path'])
    manifest = {'format':1, 'entry':'agent.mjs', 'registry':'engines.json', 'files':entries}
    if len((json.dumps(manifest, indent=2) + '\n').encode()) > 2 * 1024 * 1024:
        raise ValueError('Static client manifest exceeds 2 MiB.')
    write_json(web / 'agent-manifest.json', manifest)


def refresh_assets(root=ROOT):
    """Refresh standalone player assets while preserving renderer identities."""
    web = Path(root) / 'web'
    if (web / 'spatial.mjs').is_file():
        build = Path(root) / 'build/wasm'
        try:
            spatial = json.loads((build / 'spatial.json').read_text())
            if not isinstance(spatial, dict) or spatial.get('format') != 1 or any(
                spatial.get(key) != digest(build / name) for key, name in (
                    ('wasm_sha256', 'spatial.wasm'),
                    ('browser_sha256', 'browser.wasm'),
                    ('library_sha256', 'library.wasm'))):
                raise ValueError('Spatial companion does not match its build inputs.')
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError('Build the native spatial companion with make spatial-web.') from error
        shutil.copyfile(build / 'spatial.wasm', web / 'spatial-view.wasm')
        shutil.copyfile(build / 'spatial.json', web / 'spatial-view.json')
    stage_agent_references(root)
    stage_analysis(root)
    agent_manifest(web)
    files = ['./'] + [p.relative_to(web).as_posix() for p in web.rglob('*')
        if p.is_file() and p.relative_to(web).parts[0] not in ('engines', 'songs', 'catalog')
        and not any(part.startswith('.') for part in p.relative_to(web).parts)
        and p.name not in ('sw.js', 'sw.template.js', 'precache.json')]
    write_json(web / 'precache.json', sorted(files))


def engine_descriptor(text, label):
    if not isinstance(text, str):
        raise ValueError(label + ' --engine must return one ENGINE descriptor.')
    descriptor = text.strip()
    if not descriptor.startswith('ENGINE ') or not descriptor[7:].strip() or '\n' in descriptor:
        raise ValueError(label + ' --engine must return one ENGINE descriptor.')
    return descriptor[7:]


def wasm_identities(root):
    """Run both packaged executables with their actual browser host runtime."""
    script = """
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root = process.argv[1];
const {runCLI} = await import(pathToFileURL(path.join(root, 'web/runtime.mjs')).href);
const output = {};
for (const name of ['harness.wasm', 'browser.wasm']) {
    const module = await WebAssembly.compile(await fs.readFile(path.join(root, 'build/wasm', name)));
    const result = await runCLI(module, ['--engine'], {});
    if (result.code !== 0 || result.stderr.trim()) throw Error(name + ' --engine failed: ' + result.stderr);
    output[name] = result.stdout;
}
process.stdout.write(JSON.stringify(output));
"""
    rows = json.loads(subprocess.check_output(['node', '--input-type=module', '-e', script, str(root)],
                                             cwd=root, text=True))
    if not isinstance(rows, dict) or set(rows) != {'harness.wasm', 'browser.wasm'}:
        raise ValueError('Wasm identity check did not return both executable descriptors.')
    return {name: engine_descriptor(value, name) for name, value in rows.items()}


def package(root=ROOT, *, release_id=None, source=None, notices_dir=None, repository=None, public_ref=None):
    root = Path(root).resolve()
    build, web = root / 'build/wasm', root / 'web'
    harness = root / 'build/s12e10p1v1/harness'
    project_license = (root / 'LICENSE').read_bytes()
    if not project_license.strip():
        raise ValueError('A renderer archive requires the nonempty project LICENSE.')
    identity = engine_descriptor(subprocess.check_output([str(harness), '--engine'], text=True), 'Native')
    stamp_path = build / 'build.json'
    stamp = json.loads(stamp_path.read_text()) if stamp_path.exists() else {}
    version_file = build / 'toolchain.txt'
    toolchain = stamp.get('toolchain') or (version_file.read_text().strip()
        if version_file.exists() else 'unrecorded development toolchain')
    revision = None
    notices = {}
    if notices_dir is None and (build / 'notices').is_dir():
        notices_dir = build / 'notices'
    if notices_dir:
        for name in NOTICES:
            path = Path(notices_dir) / name
            content = path.read_bytes() if path.is_file() else b''
            if not content.strip():
                raise ValueError('A renderer notice must be a nonempty file: ' + name)
            notices[name] = content
    if release_id:
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})-([0-9a-f]{7,40})', release_id)
        if not match:
            raise ValueError('Release ID must be YYYY-MM-DD-shortcommit.')
        datetime.date.fromisoformat(match[1])
        revision = source or source_commit(root)
        if not revision or not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('A kept release requires its full public source commit.')
        if not revision.startswith(match[2]) or source_commit(root) != revision:
            raise ValueError('Release ID/source must match the clean current public source.')
        if stamp.get('source_commit') != revision:
            raise ValueError('Rebuild Wasm from the clean release source before packaging.')
        if stamp.get('artifacts') != {name: digest(build / name) for name in MODULES}:
            raise ValueError('Wasm artifacts differ from their clean source build.')
        if not isinstance(stamp.get('toolchain'), str) or not stamp['toolchain'].strip() or '\n' in stamp['toolchain']:
            raise ValueError('A kept release requires its recorded build toolchain.')
        for name in NOTICES:
            if name not in notices:
                raise ValueError('A kept release requires the matching toolchain notice: ' + name)
        if any(value != identity for value in wasm_identities(root).values()):
            raise ValueError('Native, harness-Wasm and browser-Wasm engine identities must match.')
        if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
            raise ValueError('A kept release requires --repository, the https URL of the public source repository.')
        public_ref = public_ref or 'origin/main'
        if not PUBLIC_REF.fullmatch(public_ref):
            raise ValueError('Invalid public ref: ' + public_ref)
        if not published_commit(root, revision, public_ref, repository):
            raise ValueError('The source commit is not in ' + public_ref + ', or that branch does not belong to a '
                             'remote that fetches from ' + repository + '. Push the commit there, fetch, and '
                             'package again; --public-ref names a branch of that remote.')
    elif source:
        raise ValueError('--source-commit requires --release-id.')
    elif repository or public_ref:
        raise ValueError(('--repository' if repository else '--public-ref') + ' requires --release-id.')

    if notices:
        (web / 'notices').mkdir(exist_ok=True)
        for name, content in notices.items():
            (web / 'notices' / name).write_bytes(content)

    runtime_files = [web / 'runtime.mjs'] + sorted(p for p in
        (web / 'vendor/browser_wasi_shim').rglob('*') if p.is_file())
    package_hash = hashlib.sha256()
    package_hash.update(b'LICENSE\0' + project_license)
    for path in [build / name for name in MODULES] + runtime_files:
        package_hash.update(path.relative_to(root).as_posix().encode() + b'\0' + path.read_bytes())
    for name, content in sorted(notices.items()):
        package_hash.update(('notices/' + name).encode() + b'\0' + content)
    engine_id = release_id or 'dev-' + package_hash.hexdigest()[:16]
    parent = web / 'engines'
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / engine_id
    registry_path = web / 'engines.json'
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {'format': 1, 'engines': []}
    old = next((e for e in registry['engines'] if e['id'] == engine_id), None)
    if old and (old.get('source_commit') != revision or old.get('identity') != identity):
        raise ValueError('Refusing to change an existing renderer source binding.')
    if old and release_id and old.get('toolchain') != toolchain:
        raise ValueError('Refusing to change an existing renderer toolchain binding.')
    if old and release_id and old.get('repository', repository) != repository:
        raise ValueError('Refusing to change an existing renderer repository binding.')
    with tempfile.TemporaryDirectory(prefix='.package-', dir=parent) as directory:
        staged = Path(directory)
        (staged / 'LICENSE').write_bytes(project_license)
        for name in MODULES:
            shutil.copyfile(build / name, staged / name)
        shutil.copyfile(web / 'runtime.mjs', staged / 'runtime.mjs')
        shutil.copytree(web / 'vendor/browser_wasi_shim', staged / 'vendor/browser_wasi_shim')
        if notices:
            (staged / 'notices').mkdir()
            for name, content in notices.items():
                (staged / 'notices' / name).write_bytes(content)
        if output.exists():
            previous = {p.relative_to(output).as_posix(): digest(p) for p in output.rglob('*') if p.is_file()}
            proposed = {p.relative_to(staged).as_posix(): digest(p) for p in staged.rglob('*') if p.is_file()}
            if previous != proposed:
                raise ValueError('Refusing to replace immutable renderer package: ' + engine_id)
        else:
            shutil.copytree(staged, output)
            output.chmod(0o755)
    entry = {
        'id': engine_id, 'release_id': release_id, 'development': not bool(release_id),
        'identity': identity, 'source_commit': revision, 'repository': repository, 'toolchain': toolchain,
        'withdrawn': False, 'withdrawn_at': None,
        'wasm': f'engines/{engine_id}/harness.wasm', 'sha256': digest(output / 'harness.wasm'),
        'size': (output / 'harness.wasm').stat().st_size,
        'browser_wasm': f'engines/{engine_id}/browser.wasm', 'browser_sha256': digest(output / 'browser.wasm'),
        'runtime_module': f'engines/{engine_id}/runtime.mjs', 'runtime_sha256': digest(output / 'runtime.mjs'),
        'library_wasm': f'engines/{engine_id}/library.wasm', 'library_sha256': digest(output / 'library.wasm'),
        'toolchain_notices': f'engines/{engine_id}/notices/' if notices_dir else None,
        'browsers_verified': []}
    if old:
        for key in ('withdrawn', 'withdrawn_at', 'browsers_verified'):
            entry[key] = old.get(key, entry[key])
    previous = [e for e in registry['engines'] if e['id'] != engine_id]
    if not release_id:
        # Generated archives are ignored by Git. A fresh checkout must not keep
        # stale development IDs whose files it cannot serve. Kept or withdrawn
        # IDs must remain explicit; their original archives require restoration.
        previous = [e for e in previous if e.get('release_id') or e.get('withdrawn')
                    or e.get('withdrawn_at') or archive_available(web, e)]
    registry['engines'] = previous + [entry]
    if not entry['withdrawn']:
        registry['default'] = engine_id
    write_json(registry_path, registry)
    shutil.copyfile(root / 'docs/INSTRUMENT.md', web / 'instrument.txt')
    shutil.copyfile(root / 'tests/golden/48000/sine.tl', web / 'example.tl')
    reference = web / 'reference'
    reference.mkdir(exist_ok=True)
    for mode in ('params', 'gen-params'):
        (reference / (mode + '.txt')).write_text(subprocess.check_output([str(harness), '--' + mode], text=True))
    worker, template = web / 'sw.js', web / 'sw.template.js'
    version = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    source = template.read_text()
    generated, replaced = re.subn(r"const RELEASE='[^']+';", "const RELEASE='" + version + "';", source)
    if replaced != 1:
        raise ValueError('Service worker template requires exactly one RELEASE declaration.')
    worker.write_text(generated)
    refresh_assets(root)
    return entry


def withdraw(root, renderer_id, date):
    datetime.date.fromisoformat(date)
    path = Path(root) / 'web/engines.json'
    registry = json.loads(path.read_text())
    entry = next((e for e in registry['engines'] if e['id'] == renderer_id), None)
    if not entry:
        raise ValueError('Unknown renderer ID.')
    entry.update(withdrawn=True, withdrawn_at=date)
    if registry.get('default') == renderer_id:
        registry['default'] = None
    write_json(path, registry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-id', help='release identifier YYYY-MM-DD-shortcommit; omitted means development')
    parser.add_argument('--source-commit', help='full public source commit for the kept release')
    parser.add_argument('--notices-dir', type=Path, help='matching wasi-libc/compiler-rt licence notices')
    parser.add_argument('--repository', help='https URL of the public source repository (kept releases)')
    parser.add_argument('--public-ref', help='remote-tracking branch that must contain the source commit; its '
                        'remote must fetch from --repository (kept releases; default origin/main)')
    parser.add_argument('--withdraw', metavar='ID', help='mark renderer unavailable without deleting its archive')
    parser.add_argument('--withdrawn-at', help='withdrawal date, YYYY-MM-DD')
    parser.add_argument('--refresh-assets', action='store_true', help='refresh precache; keep renderer registry and SW unchanged')
    args = parser.parse_args()
    try:
        if args.refresh_assets:
            if any((args.withdraw, args.withdrawn_at, args.release_id, args.source_commit, args.notices_dir,
                    args.repository, args.public_ref)):
                parser.error('--refresh-assets excludes renderer packaging and withdrawal options')
            refresh_assets()
            print('Refreshed standalone player assets.')
        elif args.withdraw:
            if (not args.withdrawn_at or args.release_id or args.source_commit or args.notices_dir
                    or args.repository or args.public_ref):
                parser.error('--withdraw requires --withdrawn-at and excludes build options')
            withdraw(ROOT, args.withdraw, args.withdrawn_at)
            print('Marked renderer withdrawn:', args.withdraw)
        else:
            if args.withdrawn_at:
                parser.error('--withdrawn-at requires --withdraw')
            entry = package(release_id=args.release_id, source=args.source_commit, notices_dir=args.notices_dir,
                            repository=args.repository, public_ref=args.public_ref)
            print('Prepared static workbench engine', entry['id'])
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
