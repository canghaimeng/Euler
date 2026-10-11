# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Filesystem isolation for team processes on Linux and macOS.

Fail closed when unavailable. This is file isolation, not a network sandbox.
Apply only inside a dedicated child process before launching provider tools.
"""
from __future__ import annotations
import ctypes
import os
from pathlib import Path
import platform
import json
import shutil
import subprocess


def checked_roots(writable, hidden=()):
    allowed = tuple(Path(p).resolve(strict=True) for p in writable)
    denied = tuple(Path(p).resolve() for p in hidden)
    if not allowed or any(p == Path('/') for p in allowed + denied):
        raise IsolationError('root-wide or empty workspace grants are forbidden')
    if any(a == d or a in d.parents or d in a.parents for a in allowed for d in denied):
        raise IsolationError('restored and source paths overlap')
    return allowed, denied


def macos_profile(writable, hidden=()):
    """Seatbelt uses canonical paths, including /private aliases and symlinks."""
    allowed, denied = checked_roots(writable, hidden)
    quote = lambda p: json.dumps(str(p), ensure_ascii=False)
    lines = ['(version 1)', '(allow default)', '(deny file-write*)']
    for p in allowed:
        lines.append(f'(allow file-write* ({"subpath" if p.is_dir() else "literal"} {quote(p)}))')
    lines.append('(allow file-write* (literal "/dev/null"))')
    for p in denied:
        lines.append(f'(deny file-read* (subpath {quote(p)}))')
    return '\n'.join(lines)


def host_support():
    """Probe the OS isolation backend, without starting a model or reading auth."""
    system = platform.system()
    if system == 'Linux' and platform.machine() in {'x86_64', 'aarch64', 'arm64'}:
        try:
            abi = ctypes.CDLL(None, use_errno=True).syscall(444, 0, 0, 1)
        except (OSError, AttributeError):
            abi = -1
        return {'supported': abi >= 3, 'platform': system, 'backend': 'landlock',
                'detail': f'Landlock ABI {abi}; ABI 3 or later required'}
    if system == 'Darwin':
        binary = shutil.which('sandbox-exec')
        result = subprocess.run([binary, '-p', '(version 1)(allow default)', '/usr/bin/true'],
                                capture_output=True, timeout=10) if binary else None
        return {'supported': result is not None and result.returncode == 0,
                'platform': system, 'backend': 'seatbelt',
                'detail': 'macOS sandbox-exec must be available and permitted'}
    return {'supported': False, 'platform': system, 'backend': None,
            'detail': 'Team execution supports Linux and macOS'}


class IsolationError(RuntimeError):pass


class Ruleset(ctypes.Structure):
    _fields_=[('handled_access_fs',ctypes.c_uint64)]


class Beneath(ctypes.Structure):
    _pack_=1
    _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]


def restrict_filesystem(writable, hidden=()):
    if platform.system()!='Linux' or platform.machine() not in {'x86_64','aarch64'}:
        raise IsolationError('isolated team execution requires a supported Linux Landlock host')
    libc=ctypes.CDLL(None,use_errno=True)
    abi=libc.syscall(444,0,0,1)
    if abi<3:raise IsolationError('Landlock ABI 3 or later is required; no unsandboxed fallback')
    all_rights=(1<<15)-1
    readonly=1|4|8
    attr=Ruleset(all_rights);rules=libc.syscall(444,ctypes.byref(attr),ctypes.sizeof(attr),0)
    if rules<0:raise IsolationError('cannot create filesystem rules')
    allowed,deny=checked_roots(writable,hidden)
    def below(a,b):return a==b or b in a.parents
    if any(below(a,d) or below(d,a) for a in allowed for d in deny):raise IsolationError('restored and source paths overlap')
    def rule(path,rights):
        fd=os.open(path,os.O_PATH|os.O_CLOEXEC)
        try:
            if not Path(path).is_dir():rights &= 1|2|4|(1<<14)
            arg=Beneath(rights,fd)
            if libc.syscall(445,rules,1,ctypes.byref(arg),0)<0:raise IsolationError('cannot apply filesystem rule')
        finally:os.close(fd)
    def read_tree(path):
        resolved=path.resolve()
        if any(below(resolved,d) for d in deny):return
        if any(below(d,resolved) for d in deny):
            # Importers must list ancestor directories. This grants names only;
            # source file contents and all source writes remain denied.
            rule(path,8)
            for child in path.iterdir():
                if not child.is_symlink():read_tree(child)
                else:
                    dest=child.resolve()
                    if not any(below(dest,d) or below(d,dest) for d in deny):rule(child,readonly)
        else:rule(path,readonly)
    try:
        read_tree(Path('/'))
        for path in allowed:rule(path,all_rights)
        rule('/dev/null',2|4)
        if libc.prctl(38,1,0,0,0)<0 or libc.syscall(446,rules,0)<0:
            raise IsolationError('kernel refused team filesystem isolation')
    finally:os.close(rules)


def main():
    import argparse,json
    parser=argparse.ArgumentParser();parser.add_argument('--policy',required=True);parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();policy=json.loads(Path(args.policy).read_text())
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:raise IsolationError('missing isolated command')
    if platform.system() == 'Darwin':
        binary=shutil.which('sandbox-exec')
        if not binary:raise IsolationError('macOS sandbox-exec unavailable; no unsandboxed fallback')
        command=[binary,'-p',macos_profile(policy['writable'],policy.get('hidden',[])),*command]
    else:
        restrict_filesystem(policy['writable'],policy.get('hidden',[]))
    os.execvpe(command[0],command,os.environ)

if __name__=='__main__':main()
