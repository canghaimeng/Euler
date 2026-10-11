# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Private local Git snapshots of complete project files, not graph summaries.

Snapshots are admitted only at an idle boundary. Source Git indexes are never
touched. Worker notes are copied opaquely; browser APIs expose public projections
only. Credentials, process handles and reproducible dependency caches are not
portable state. Every omission and external link is recorded in the manifest.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time


CONFIG = '.euler-milestones.json'
OMIT_DIRS = {'.git', '.lake', '.venv', 'venv', 'node_modules', '__pycache__',
             '.pytest_cache', '.codex', '.grok', '.cursor', '.claude', '.ssh',
             '.cursor-runtime', '.grok-home', '.codex-home', '.npm', '.cache', '.euler-provider'}
OMIT_NAMES = {'.pid', '.pid.lock', '.stop', '.run_deadline', 'auth.json',
              'credentials.json', CONFIG, 'human-admission.lock'}


class MilestoneError(ValueError):
    pass


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def _hash(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _git(repo, *args, input=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
        'user.name=Euler workspace', '-c', 'user.email=workspace@localhost',
        '-C', str(repo), *args], input=input, env=env, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


@contextmanager
def _lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    with (root / 'lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _omission(path):
    if path.name in OMIT_DIRS or path.name in OMIT_NAMES:
        return 'credentials/provider cache/process state or reproducible cache'
    if path.name.startswith('.env') and not path.name.endswith('.example'):
        return 'local environment credentials'
    if path.suffix.lower() in {'.pem', '.key', '.p12'}:
        return 'credential file'
    if path.name.endswith(('-wal', '-shm', '.lock')):
        return 'transient lock or SQLite sidecar'
    return None


def _inventory(roots):
    files, skipped, links = {}, [], []
    for alias, root in roots.items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', alias):
            raise MilestoneError('invalid attachment name')
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise MilestoneError('snapshot roots must be directories')
        for folder, dirs, names in os.walk(root, followlinks=False):
            for name in list(dirs) + names:
                path = Path(folder) / name
                relative = str(Path(alias) / path.relative_to(root))
                reason = _omission(path)
                if reason:
                    skipped.append({'path': relative, 'reason': reason})
                    if name in dirs:
                        dirs.remove(name)
                    continue
                if path.is_symlink():
                    links.append({'path': relative, 'target': str(path.resolve()),
                                  'original': os.readlink(path)})
                    if name in dirs:
                        dirs.remove(name)
                    continue
                if path.is_dir():
                    continue
                s = path.stat()
                if not stat.S_ISREG(s.st_mode):
                    skipped.append({'path': relative, 'reason': 'non-regular file'})
                    continue
                files[relative] = (path, (s.st_size, s.st_mtime_ns, s.st_ino), stat.S_IMODE(s.st_mode))
    return files, skipped, links


def _directories(roots):
    result = []
    for alias, root in roots.items():
        root = Path(root).resolve()
        for folder, dirs, _ in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not _omission(Path(folder)/d) and not (Path(folder)/d).is_symlink()]
            result.append(str(Path(alias)/Path(folder).relative_to(root)))
    return sorted(result)


def _wal_versions(roots):
    versions = {}
    files, _, _ = _inventory(roots)
    for name, (path, _, _) in files.items():
        wal = path.with_name(path.name + '-wal')
        if wal.is_file():
            s = wal.stat()
            if s.st_size:
                versions[name] = (s.st_size, s.st_mtime_ns)
    return versions


def _idle(project, *, settled_worker_pid=None):
    from euler.control.project import is_euler_project, open_project
    from euler.control.campaign import ACTIVE
    if is_euler_project(project):
        with open_project(project) as p:
            if any(l.status in ACTIVE for l in p.runtime_snapshot().leases):
                raise MilestoneError('active leases: wait for team settlement before capturing')
    for pidfile in (project / 'workers').glob('*/.pid'):
        try:
            pid = int(pidfile.read_text().strip())
            if pid <= 1:
                continue
            # Only the calling, settled worker can exempt itself during its
            # synchronous checkpoint epilogue. Other workers still block capture.
            if pid == settled_worker_pid == os.getpid():
                continue
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, FileNotFoundError):
            continue
        except PermissionError:
            pass
        raise MilestoneError('worker process is still present; wait for a quiescent team')


def configured(project):
    path = Path(project) / CONFIG
    if path.is_file() and not path.is_symlink():
        return json.loads(path.read_text())
    return None


def enable(project, *, store=None, attachments=None, dashboard=None):
    project = Path(project).resolve(strict=True)
    store = Path(store or project.parent / '.euler-history' / project.name).resolve()
    if store == project or project in store.parents:
        raise MilestoneError('history store must be outside the project')
    roots = {'project': str(project), **(attachments or {})}
    if roots['project'] != str(project):
        raise MilestoneError('the project root cannot be replaced by an attachment')
    for alias, root in roots.items():
        r = Path(root).resolve(strict=True)
        if r == store or store in r.parents or r in store.parents:
            raise MilestoneError('history store and captured roots must be disjoint')
    settings = {'store': str(store), 'roots': roots, 'dashboard': str(dashboard) if dashboard else None}
    _json(project / CONFIG, settings)
    return settings


class Milestones:
    def __init__(self, project, *, settings=None):
        self.project = Path(project).resolve(strict=True)
        self.settings = settings or configured(self.project)
        if not self.settings:
            raise MilestoneError('milestones have not been enabled for this project')
        self.store = Path(self.settings['store']).resolve()
        self.repo = self.store / 'repository'

    def _ensure(self):
        if not self.repo.exists():
            self.repo.mkdir(mode=0o700)
            _git(self.repo, 'init', '--quiet')
            _git(self.repo, 'config', 'core.autocrlf', 'false')
        # Preserve byte contents even if a source tree ships Git filters or
        # export-ignore rules. Snapshot storage is not the source repository.
        (self.repo/'.git/info/attributes').write_text('* -filter -text -export-ignore -export-subst\n')

    def list(self):
        if not (self.repo / '.git').is_dir():
            return []
        try:
            commits = _git(self.repo, 'rev-list', '--max-count=100', 'HEAD').splitlines()
        except subprocess.CalledProcessError:
            return []
        return [self.describe(c) for c in commits]

    def manifest(self, commit):
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise MilestoneError('an exact Git commit is required')
        try:
            return json.loads(_git(self.repo, 'show', commit + ':manifest.json'))
        except (subprocess.CalledProcessError, ValueError) as exc:
            raise MilestoneError('unknown or invalid milestone') from exc

    def describe(self, commit):
        m = self.manifest(commit)
        # Never expose worker-private paths, contents, or host credentials.
        return {'commit': commit, **{k: m[k] for k in ['label', 'created_at', 'file_count',
            'bytes', 'graph_counts', 'kind']}, 'excluded_count': len(m['excluded']),
            'external_links': len([l for l in m['links'] if not l.get('captured_target')]),
            'team': {k:v for k,v in m.get('team',{'available':False}).items() if k!='files'}}

    def capture(self, label, *, key, projection=None, kind='milestone', _settled_worker_pid=None):
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 180 or not key:
            raise MilestoneError('a short label and idempotency key are required')
        from euler.control.project import is_euler_project
        from euler.control.campaign import admission_lock
        from .team import load as load_team, TEAM, capture_team
        guard = admission_lock(self.project) if is_euler_project(self.project) else nullcontext()
        team_guard = _lock(self.project/TEAM) if load_team(self.project) else nullcontext()
        with guard, team_guard, _lock(self.store):
            self._ensure()
            for record in self.list():
                m = self.manifest(record['commit'])
                if m['key'] == key:
                    if m['label'] != label or m['kind'] != kind:
                        raise MilestoneError('milestone idempotency key has different content')
                    return record
            _idle(self.project, settled_worker_pid=_settled_worker_pid)
            roots = self.settings['roots']
            before, excluded, links = _inventory(roots)
            directories = _directories(roots)
            wal_versions = _wal_versions(roots)
            with tempfile.TemporaryDirectory(dir=self.store, prefix='capture-') as tmp:
                stage = Path(tmp)
                records = {}
                for relative, (src, _, mode) in before.items():
                    dest = stage / 'files' / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with src.open('rb') as stream:
                        sqlite = stream.read(16) == b'SQLite format 3\x00'
                    if sqlite:
                        with sqlite3.connect(src.as_uri() + '?mode=ro', uri=True) as origin:
                            with sqlite3.connect(dest) as target:
                                origin.backup(target)
                    else:
                        shutil.copyfile(src, dest)
                    dest.chmod(mode & 0o777)
                    records[relative] = {'sha256': _hash(dest), 'bytes': dest.stat().st_size, 'mode': mode}
                for link in links:
                    for alias, root in roots.items():
                        try:
                            rel = Path(link['target']).relative_to(Path(root).resolve())
                        except ValueError:
                            continue
                        target = str(Path(alias) / rel)
                        if target in records or any(p.startswith(target.rstrip('/') + '/') for p in records):
                            link['captured_target'] = target
                            break
                public = projection() if projection else None
                team_checkpoint = capture_team(self.project,stage)
                if public is not None:
                    _json(stage / 'view/snapshot.json', public)
                after, _, after_links = _inventory(roots)
                if ({k:v[1] for k,v in before.items()} != {k:v[1] for k,v in after.items()}
                    or directories != _directories(roots) or wal_versions != _wal_versions(roots)
                    or [(l['path'],l['target']) for l in links] != [(l['path'],l['target']) for l in after_links]):
                    raise MilestoneError('team files changed during capture; retry at the next idle boundary')
                _idle(self.project, settled_worker_pid=_settled_worker_pid)
                m = {'schema': 1, 'key': key, 'label': label, 'kind': kind,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'source_project': str(self.project),
                    'roots': roots, 'files': records, 'directories':directories, 'links': links, 'excluded': excluded,
                    'team':team_checkpoint,
                    'file_count': len(records), 'bytes': sum(r['bytes'] for r in records.values()),
                    'graph_counts': {layer: {'nodes':len(public[layer+'_graph']['nodes']),
                        'edges':len(public[layer+'_graph']['edges'])} for layer in ['facts','research']} if public else {},
                    'boundary': 'Idle local file state; provider credentials, remote model sessions and processes require fresh authorization.'}
                _json(stage / 'manifest.json', m)
                for name in ['files', 'view', 'team', 'manifest.json']:
                    old = self.repo / name
                    if old.is_dir():
                        shutil.rmtree(old)
                    elif old.exists():
                        old.unlink()
                    if (stage / name).exists():
                        shutil.move(str(stage / name), old)
                _git(self.repo, 'add', '-f', '--all', '--', '.')
                _git(self.repo, 'commit', '--quiet', '-m', 'milestone: ' + label)
                return self.describe(_git(self.repo, 'rev-parse', 'HEAD'))

    def restore(self, commit, *, key, question='', object_id=None, layer='research'):
        if not isinstance(question, str) or len(question) > 12000 or not key:
            raise MilestoneError('invalid question or request identity')
        m = self.manifest(commit)
        payload = {'commit':commit, 'key':key, 'question':question, 'object_id':object_id, 'layer':layer}
        identity = hashlib.sha256(key.encode()).hexdigest()[:20]
        destination = self.store / 'restored' / identity
        with _lock(self.store):
            if (destination / 'RESTORE.json').exists():
                result = json.loads((destination / 'RESTORE.json').read_text())
                if result['request'] != payload:
                    raise MilestoneError('restore idempotency key has different content')
                root = Path(result['project_dir'])
                if not configured(root):
                    _json(root/CONFIG, {'store':str(destination/'history'),
                        'roots':{alias:str(destination/'files'/alias) for alias in m['roots']},'dashboard':None})
                return result
            if destination.exists():
                raise MilestoneError('incomplete restore directory; operator recovery required')
            with tempfile.TemporaryDirectory(dir=self.store, prefix='restore-') as tmp:
                stage = Path(tmp)
                # checkout-index never executes filters or restores source .git metadata.
                archive = subprocess.Popen(['git', '-C', str(self.repo), 'archive', commit], stdout=subprocess.PIPE)
                import tarfile
                try:
                    with tarfile.open(fileobj=archive.stdout, mode='r|') as tar:
                        for member in tar:
                            rel = Path(member.name)
                            if rel.is_absolute() or '..' in rel.parts or not (member.isfile() or member.isdir()):
                                raise MilestoneError('unsafe entry in milestone archive')
                            target = stage / rel
                            if member.isdir():
                                target.mkdir(parents=True, exist_ok=True)
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                with tar.extractfile(member) as source, target.open('wb') as out:
                                    shutil.copyfileobj(source, out)
                                target.chmod(member.mode & 0o777)
                finally:
                    archive.stdout.close()
                    archive.wait()
                if archive.returncode:
                    raise MilestoneError('Git restore failed')
                for directory in m.get('directories', []):
                    (stage / 'files' / directory).mkdir(parents=True, exist_ok=True)
                for relative, record in m['files'].items():
                    if _hash(stage / 'files' / relative) != record['sha256']:
                        raise MilestoneError('restored file hash mismatch')
                for link in m['links']:
                    if link.get('captured_target'):
                        at = stage / 'files' / link['path']
                        at.parent.mkdir(parents=True, exist_ok=True)
                        at.symlink_to(os.path.relpath(stage / 'files' / link['captured_target'], at.parent))
                if object_id:
                    view = stage / 'view/snapshot.json'
                    if not view.exists() or layer not in {'facts', 'research'} or object_id not in {n['id'] for n in json.loads(view.read_text())[layer+'_graph']['nodes']}:
                        raise MilestoneError('selected object does not exist in this exact milestone')
                selected_kind = None
                if object_id:
                    selected_kind = next(n.get('kind','obligation') for n in json.loads(view.read_text())[layer+'_graph']['nodes'] if n['id']==object_id)
                    if selected_kind == 'failure':
                        selected_kind = 'blocker'
                root = stage / 'files/project'
                root.mkdir(parents=True, exist_ok=True)
                from .team import restore_team
                team_result = restore_team(stage,destination,root,commit,m)
                from euler.control.project import is_euler_project, open_project
                if is_euler_project(root):
                    from euler.control.campaign import CampaignControl, campaign_snapshot
                    with open_project(root) as p:
                        state = campaign_snapshot(p)
                        if state['active_leases']:
                            raise MilestoneError('snapshot with active leases cannot be resumed safely')
                        CampaignControl(p, actor='human:milestone-restore').command('pause', mode='drain',
                            key='restore:' + identity, expected_revision=state['revision'], contract_id=state['contract_id'])
                result = {'request': payload, 'source_commit': commit, 'source_label': m['label'],
                    'created_at': datetime.now(timezone.utc).isoformat(), 'state':'RESTORED_AWAITING_RESEARCH',
                    'project_dir': str(destination / 'files/project'), 'file_count': m['file_count'],
                    'excluded_count':len(m['excluded']), 'external_links':len([l for l in m['links'] if not l.get('captured_target')]),
                    'selected_object_kind':selected_kind, 'team':team_result,
                    'provider_session_resumed':False, 'worker_started':False}
                _json(stage / 'RESTORE.json', result)
                _json(root / 'RESTORE_ORIGIN.json', result)
                # Continued research gets its own history, never commits into
                # the source campaign's history or points back at live files.
                _json(root / CONFIG, {'store':str(destination/'history'),
                    'roots':{alias:str(destination/'files'/alias) for alias in m['roots']},
                    'dashboard':None})
                if question.strip():
                    (root / 'FOLLOWUP.md').write_text('# Research question from a restored milestone\n\n'
                        + 'Source Git commit: `' + commit + '`\n\n' + 'Selected object: ' + str(object_id or '(whole project)')
                        + '\n\n## User question\n\n' + question + '\n\n## Continuation requirements\n\n'
                        + 'Read the restored project goal, shared records, proof files and team assignments. '
                        + 'All captured files are available in this workspace; this is not a one-node task fixture. '
                        + 'Do not reinterpret this request as a verified Fact. Preserve the original scope, '
                        + 'use existing verification gates, and obtain a fresh execution allowance before launching workers.\n')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(stage), destination)
                return result


def public_projection(project, dashboard=None):
    from euler.observability.monitor import dashboard_config, snapshot
    with dashboard_config(Path(dashboard) if dashboard else None):
        result = snapshot(Path(project), use_cache=False)
    if dashboard and json.loads(Path(dashboard).read_text()).get('case_study_id'):
        if result['research_graph'].get('projection') != 'historical_source':
            raise MilestoneError('historical case projection unavailable; configure its source manifest before capture')
        from euler.observability.case_study import get_source
        case = result['research_graph']['case_id']
        identifiers = {sid for document in result.get('historical_documents',{}).values() for sid in document['sources']}
        result['historical_sources'] = {sid:get_source(case,sid) for sid in sorted(identifiers)}
        if any(source is None or source.get('integrity') != 'matched' for source in result['historical_sources'].values()):
            raise MilestoneError('historical proof source is missing or changed; snapshot refused')
    return result


def auto_capture(project, *, settled_worker_pid=None):
    """Poll meaningful public state; a changing/busy workspace is deferred."""
    settings = configured(project)
    if not settings:
        return None
    projection = public_projection(project, settings.get('dashboard'))
    from .team import load as load_team
    team_state=load_team(project)
    significant={k:projection[k] for k in ['facts_graph','research_graph']}
    if team_state:
        significant['team']={k:team_state[k] for k in ['instance_id','members','messages','state','turns_used']}
    # A completed conversation is a recoverable team milestone, not a claim
    # of mathematical progress. Unregistered projects retain graph-only triggers.
    identity = hashlib.sha256(json.dumps(significant,sort_keys=True).encode()).hexdigest()
    history = Milestones(project, settings=settings)
    if any(history.manifest(r['commit'])['key'] == 'auto:' + identity for r in history.list()):
        return None
    return history.capture('Team state checkpoint' if team_state else 'Research state checkpoint', key='auto:' + identity,
        projection=lambda: public_projection(project, settings.get('dashboard')), kind='automatic',
        _settled_worker_pid=settled_worker_pid)


def checkpoint_if_enabled(project, *, settled_worker_pid=None):
    """Execution epilogue: preserve a deferred/error record, never fake success."""
    if not configured(project):
        return
    try:
        result = auto_capture(project, settled_worker_pid=settled_worker_pid)
        status = {'state':'saved' if result else 'unchanged', 'milestone':result}
    except (MilestoneError, OSError, subprocess.CalledProcessError) as exc:
        status = {'state':'deferred', 'reason':str(exc)}
    # Status lives outside the captured roots so polling cannot trigger itself.
    _json(Path(configured(project)['store'])/'checkpoint-status.json',status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['enable','capture','list','restore','watch'])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--store', type=Path)
    parser.add_argument('--dashboard', type=Path)
    parser.add_argument('--attachment', action='append', default=[], metavar='NAME=PATH')
    parser.add_argument('--label', default='Research milestone')
    parser.add_argument('--commit')
    parser.add_argument('--key')
    parser.add_argument('--question', default='')
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    if args.command == 'enable':
        print(json.dumps(enable(args.project, store=args.store, dashboard=args.dashboard,
                               attachments=dict(a.split('=',1) for a in args.attachment))))
    elif args.command == 'watch':
        if args.interval < 1:
            parser.error('interval must be at least one second')
        while True:
            try:
                result = auto_capture(args.project)
                if result:
                    print(json.dumps(result), flush=True)
            except (MilestoneError, OSError) as exc:
                print(json.dumps({'deferred': str(exc)}), flush=True)
            time.sleep(args.interval)
    else:
        h = Milestones(args.project)
        if args.command == 'list':
            result = h.list()
        elif args.command == 'capture':
            result = h.capture(args.label, key=args.key or str(time.time_ns()),
                projection=lambda: public_projection(args.project,h.settings.get('dashboard')))
        else:
            result = h.restore(args.commit, key=args.key or str(time.time_ns()), question=args.question)
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
