"""Agent signal screen command line. Only run/demo --usb talk to the display."""
from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from state import connect, hook_event, snapshot, write_json

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / '.local' / 'agent-screen'


def vendor_running():
    import psutil
    return [p.info['pid'] for p in psutil.process_iter(['pid', 'name'])
            if (p.info['name'] or '').lower() in ('jonsbo-aio.exe', 'jonsbocanvas.exe', 'infopanel.exe')]


def acquire_mutex():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, 'Local\\AgentScreenDS339')
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
        raise RuntimeError('Another Agent Screen instance already owns the screen')
    return kernel, handle


class _SessionLevel1(ctypes.Structure):  # WTSINFOEX_LEVEL1_W
    _fields_ = [('session', wintypes.ULONG), ('state', ctypes.c_int), ('flags', wintypes.LONG),
                ('station', wintypes.WCHAR * 33), ('user', wintypes.WCHAR * 21), ('domain', wintypes.WCHAR * 18),
                ('times', ctypes.c_longlong * 5), ('counters', wintypes.DWORD * 6)]


class _SessionInfo(ctypes.Structure):  # WTSINFOEXW
    _fields_ = [('level', wintypes.DWORD), ('data', _SessionLevel1)]


def console_locked():
    """True unless the physical console shows this user's unlocked desktop. Errors count as locked."""
    try:
        kernel = ctypes.WinDLL('kernel32')
        wts = ctypes.WinDLL('wtsapi32')
        kernel.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                                    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        console = kernel.WTSGetActiveConsoleSessionId()
        if console == 0xFFFFFFFF:  # Console between sessions; this value would also mean "current session".
            return True
        buffer, size = ctypes.c_void_p(), wintypes.DWORD()
        if not wts.WTSQuerySessionInformationW(None, console, 25, ctypes.byref(buffer), ctypes.byref(size)):
            return True  # 25 = WTSSessionInfoEx
        try:
            info = ctypes.cast(buffer, ctypes.POINTER(_SessionInfo)).contents.data
            # Active (not taken over by Remote Desktop), unlocked (flag 1; reversed only on Windows 7), and this user.
            return not (info.state == 0 and info.flags == 1 and info.user.lower() == getpass.getuser().lower())
        finally:
            wts.WTSFreeMemory(buffer)
    except Exception:
        return True


def save_frame(image, root):
    path = root / 'frame.png'
    tmp = root / 'frame.tmp'
    image.save(tmp, format='PNG')
    tmp.replace(path)


def run(args):
    from render import render, demo_data
    from watch import Watcher
    root = args.data_root
    root.mkdir(parents=True, exist_ok=True)
    kernel, handle = acquire_mutex()
    db = connect(root)
    watcher = Watcher()
    device = None
    started = time.time()
    next_retry = 0
    last_error = None
    frames = 0
    running = True
    try:
        while not args.seconds or time.time() - started < args.seconds:
            tick = time.monotonic()
            stop_file = root / 'stop.json'
            if stop_file.exists() and stop_file.stat().st_mtime > started:
                break
            if args.command == 'demo':
                data = demo_data(args.state)
            else:
                watcher.poll(db)
                data = {**snapshot(db), 'private': console_locked()}
            image = render(data, demo=args.command == 'demo')
            try:
                save_frame(image, root)
            except OSError:
                pass  # A reader can briefly lock the preview; skip it rather than blank the screen.
            if args.usb and device is None and time.time() >= next_retry:
                try:
                    owners = vendor_running()
                    if owners:
                        raise RuntimeError('Close the existing JONSBO/InfoPanel app before USB takeover')
                    from ds339 import DS339
                    device = DS339().open()
                    last_error = None
                except Exception as exc:
                    last_error = f'{type(exc).__name__}: {exc}'
                    device = None
                    next_retry = time.time() + 10
            if device is not None:
                try:
                    device.send(image, args.rotation)
                    frames += 1
                except Exception as exc:
                    last_error = f'{type(exc).__name__}: {exc}'
                    try:
                        device.close()
                    except Exception:
                        pass
                    device = None
                    next_retry = time.time() + 10
            try:
                write_json(root / 'status.json', {**data, 'pid': os.getpid(), 'running': running,
                           'usb_connected': device is not None, 'frames_sent': frames,
                           'last_error': last_error, 'demo': args.command == 'demo', 'started': started})
            except OSError:
                pass  # Same for status.json readers; the next second rewrites it.
            time.sleep(max(.01, 1 - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    finally:
        if device:
            try:
                device.close(power_off=True)
            except Exception:
                pass
        db.close()
        write_json(root / 'status.json', {'pid': os.getpid(), 'running': False,
                   'usb_connected': False, 'frames_sent': frames, 'last_error': last_error,
                   'updated': time.time(), 'demo': args.command == 'demo'})
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('status')
    commands.add_parser('stop')
    preview = commands.add_parser('preview')
    preview.add_argument('--state', choices=['working', 'waiting', 'done', 'idle', 'unknown', 'error'], default='working')
    for name in ('run', 'demo'):
        sub = commands.add_parser(name)
        sub.add_argument('--usb', action='store_true')
        sub.add_argument('--seconds', type=float, default=0)
        sub.add_argument('--rotation', type=int, choices=[90, 270], default=90)
        if name == 'demo':
            sub.add_argument('--state', choices=['working', 'waiting', 'done'], default='working')
    hook = commands.add_parser('hook')
    hook.add_argument('--agent', choices=['codex', 'claude'], required=True)
    hook.add_argument('--event')
    args = parser.parse_args()
    root = args.data_root.resolve()
    args.data_root = root
    root.mkdir(parents=True, exist_ok=True)
    if args.command == 'hook':
        # A broken display must not block or change an agent's permission decision.
        try:
            payload = json.loads(sys.stdin.buffer.read(4 * 1024 * 1024))
            db = connect(root)
            hook_event(db, args.agent, payload, args.event)
            db.close()
        except Exception:
            pass
        return
    if args.command == 'doctor':
        from ds339 import inventory
        print(json.dumps({**inventory(), 'vendor_processes': vendor_running(),
                          'data_root': str(root)}, indent=2))
    elif args.command == 'status':
        path = root / 'status.json'
        print(path.read_text(encoding='utf-8') if path.exists() else '{"running":false}')
    elif args.command == 'stop':
        write_json(root / 'stop.json', {'requested': time.time()})
    elif args.command == 'preview':
        from render import render, demo_data
        save_frame(render(demo_data(args.state), demo=True), root)
        print(str(root / 'frame.png'))
    else:
        run(args)


if __name__ == '__main__':
    main()
