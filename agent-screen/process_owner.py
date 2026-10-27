"""Read-only Windows process identities for native terminal agent sessions.

PID plus process creation time prevents confusing a reused PID with the owner.
Hook-side functions use only the standard library; handle discovery is a daemon
fallback for sessions that were already open when monitoring started.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


class ProcessEntry(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                ('flags', wintypes.DWORD), ('exe', wintypes.WCHAR * 260)]


@lru_cache(maxsize=1)
def kernel():
    dll = ctypes.WinDLL('kernel32', use_last_error=True)
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    dll.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    dll.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    dll.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    dll.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    dll.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    return dll


def process_birth(pid):
    """Creation FILETIME if alive, 0 if gone, None if inspection is unavailable."""
    if os.name != 'nt':
        return None
    dll = kernel()
    handle = dll.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION only
    if not handle:
        return 0 if ctypes.get_last_error() in (87, 1168) else None
    try:
        code = wintypes.DWORD()
        if not dll.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != 259:  # STILL_ACTIVE
            return 0
        creation, end, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
        if not dll.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(end),
                                   ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    finally:
        dll.CloseHandle(handle)


def process_matches(pid, birth):
    current = process_birth(pid)
    return None if current is None else current == birth


def current_owner(agent):
    """Find the actual agent ancestor, never the short-lived hook shell itself."""
    if os.name != 'nt' or agent not in ('codex', 'claude'):
        return None
    dll = kernel()
    handle = dll.CreateToolhelp32Snapshot(2, 0)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        parents = {}
        ok = dll.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            parents[entry.pid] = (entry.parent, entry.exe.lower())
            ok = dll.Process32NextW(handle, ctypes.byref(entry))
    finally:
        dll.CloseHandle(handle)
    pid = os.getpid()
    for _ in range(32):
        parent, name = parents.get(pid, (0, ''))
        if name == agent + '.exe':
            birth = process_birth(pid)
            return (pid, birth) if birth else None
        if not parent or parent == pid:
            break
        pid = parent
    return None


def discover_codex_owners():
    """Positive associations only; missing/inaccessible handles never imply exit."""
    import psutil
    pattern = re.compile(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$', re.I)
    found = []
    for proc in psutil.process_iter(['pid', 'name']):
        if (proc.info['name'] or '').lower() != 'codex.exe':
            continue
        try:
            args = proc.cmdline()[1:]
            if any(x in ('app-server', 'exec-server', 'mcp-server') for x in args[:4]):
                continue
            birth = process_birth(proc.pid)
            if not birth:
                continue
            for file in proc.open_files():
                path = Path(file.path)
                match = pattern.search(path.name)
                if match and path.name.startswith('rollout-'):
                    found.append(('codex', match.group(1), (proc.pid, birth)))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, MemoryError):
            continue  # open_files() copies the whole system handle table; that fails when memory runs out.
    return found


def discover_claude_owners(home):
    """Interactive Claude Code processes, from Claude's own registry ~/.claude/sessions/<pid>.json.

    Covers sessions whose SessionStart hook never ran (a resume while the machine is busy
    after a restart). A file left by an earlier process with the same PID fails the birth check.
    """
    found = []
    for path in (Path(home) / '.claude' / 'sessions').glob('*.json'):
        try:
            if path.stat().st_size > 65536:
                continue
            info = json.loads(path.read_text(encoding='utf-8'))
            pid, sid = int(info.get('pid') or path.stem), str(info.get('sessionId') or '')
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if info.get('kind') != 'interactive' or not sid:
            continue
        birth = process_birth(pid)
        if birth and str(info.get('procStart', '')) == str(birth):
            found.append(('claude', sid, (pid, birth), Path(str(info.get('cwd') or '')).name))
    return found


class ProcessMonitor:
    def __init__(self, home=None):
        self.home = Path.home() if home is None else Path(home)
        self.last_discovery = 0

    def poll(self, db):
        from state import bind_owner, reconcile_owners
        now = time.time()
        if os.name == 'nt' and now - self.last_discovery >= 15:
            for agent, sid, owner in discover_codex_owners():
                bind_owner(db, agent, sid, owner, source='open-transcript')
            for agent, sid, owner, project in discover_claude_owners(self.home):
                bind_owner(db, agent, sid, owner, source='session-registry', project=project)
            self.last_discovery = now
        reconcile_owners(db, process_matches)
