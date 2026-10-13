"""Best-effort adapters for existing sessions. Official hooks remain preferred.

Logs are read locally and only lifecycle metadata reaches the state database.
The log format is not an API; silence is never treated as successful completion.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from state import record, set_title
from process_owner import ProcessMonitor


def timestamp(value, default=None):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return time.time() if default is None else default


class Tail:
    def __init__(self, path, agent):
        self.path, self.agent = path, agent
        self.offset = 0
        self.sid = path.stem
        self.cwd = ''
        self.turn_id = ''
        self.title = None
        self.initial = True
        self.ignored = False

    def poll(self, db):
        if self.ignored:
            return
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset, self.initial = 0, True
        with self.path.open('rb') as f:
            if self.initial:
                # Session identity is in the first record, which can also contain long instructions.
                first = f.readline(4 * 1024 * 1024)
                try:
                    obj = json.loads(first)
                    if self.agent == 'codex' and obj.get('type') == 'session_meta':
                        p = obj.get('payload', {})
                        source = p.get('source', {})
                        if isinstance(source, dict) and 'subagent' in source:
                            self.offset = size
                            self.initial = False
                            self.ignored = True
                            return
                        self.sid = p.get('id') or p.get('session_id') or self.sid
                        self.cwd = p.get('cwd', '')
                    elif self.agent == 'claude':
                        self.sid = obj.get('sessionId') or self.sid
                        self.cwd = obj.get('cwd', '')
                except (ValueError, TypeError):
                    pass
                self.offset = max(0, size - 512 * 1024)
                f.seek(self.offset)
                if self.offset:
                    f.readline()  # Skip a possibly partial leading JSON record.
                    self.offset = f.tell()
                self.initial = False
            else:
                f.seek(self.offset)
            # Bound each poll; leave an incomplete line for the next iteration.
            consumed = 0
            while consumed < 8 * 1024 * 1024:
                line = f.readline(8 * 1024 * 1024)
                if not line or not line.endswith(b'\n'):
                    break
                consumed += len(line)
                self.offset = f.tell()
                try:
                    obj = json.loads(line)
                    self.consume(db, obj)
                except (ValueError, TypeError, KeyError):
                    continue

    def consume(self, db, obj):
        at = timestamp(obj.get('timestamp'))
        if at < time.time() - 86400:
            return
        event, tool = '', ''
        if self.agent == 'codex':
            p = obj.get('payload', {})
            kind = p.get('type')
            if obj.get('type') == 'event_msg':
                if kind in ('task_started', 'task_complete', 'turn_aborted'):
                    event = kind
                    if kind == 'task_started':
                        self.turn_id = p.get('turn_id') or ''
                elif kind == 'user_message':
                    event = 'user_message'
            elif obj.get('type') == 'response_item':
                if kind in ('function_call', 'custom_tool_call'):
                    event, tool = 'PreToolUse', p.get('name', '')
                elif kind in ('function_call_output', 'custom_tool_call_output', 'reasoning'):
                    event = 'activity'
                elif kind == 'message' and p.get('role') == 'assistant':
                    event = 'activity'
                elif kind == 'message' and p.get('role') == 'user':
                    event = 'user_message'
        else:
            if obj.get('isSidechain'):
                return
            self.sid = obj.get('sessionId') or self.sid
            self.cwd = obj.get('cwd') or self.cwd
            if obj.get('type') in ('ai-title', 'custom-title'):
                # Claude Code re-appends its title near the end of the log; /rename writes custom-title.
                manual = obj['type'] == 'custom-title'
                title = (obj.get('customTitle' if manual else 'aiTitle'), manual)
                if title != self.title:
                    self.title = title
                    set_title(db, 'claude', self.sid, *title)
                return
            msg = obj.get('message', {})
            if obj.get('type') == 'user':
                content = msg.get('content', [])
                is_tool = isinstance(content, list) and any(x.get('type') == 'tool_result' for x in content if isinstance(x, dict))
                event = 'PostToolUse' if is_tool else 'user_message'
            elif obj.get('type') == 'assistant':
                event = 'Stop' if msg.get('stop_reason') == 'end_turn' else 'activity'
                for part in msg.get('content', []):
                    if isinstance(part, dict) and part.get('type') == 'tool_use' and part.get('name') == 'AskUserQuestion':
                        event = 'ask_sync'
        if event:
            record(db, agent=self.agent, sid=self.sid, event=event, cwd=self.cwd,
                   turn_id=self.turn_id, at=at, origin='local-session', tool=tool)


class CodexNames:
    """Codex appends each thread name (automatic or /rename) to one index; the last entry wins."""
    def __init__(self, path):
        self.path, self.offset, self.names = path, 0, {}

    def poll(self, db):
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return
        latest = {}
        with self.path.open('rb') as f:
            f.seek(self.offset)
            for line in f:
                if not line.endswith(b'\n'):
                    break
                self.offset += len(line)
                try:
                    obj = json.loads(line)
                    latest[str(obj['id'])] = obj['thread_name']
                except (ValueError, TypeError, KeyError):
                    continue
        for sid, name in latest.items():
            if name and self.names.get(sid) != name:
                self.names[sid] = name
                set_title(db, 'codex', sid, name)


class Watcher:
    def __init__(self, home=None):
        self.home = Path.home() if home is None else Path(home)
        self.tails = {}
        self.last_scan = 0
        self.codex_names = CodexNames(self.home / '.codex' / 'session_index.jsonl')
        self.process_monitor = ProcessMonitor()

    def poll(self, db):
        now = time.time()
        if now - self.last_scan > 10:
            paths = []
            codex = self.home / '.codex' / 'sessions'
            claude = self.home / '.claude' / 'projects'
            for root, pattern, agent in ((codex, '**/*.jsonl', 'codex'), (claude, '*/*.jsonl', 'claude')):
                if root.exists():
                    for path in root.glob(pattern):
                        try:
                            mtime = path.stat().st_mtime
                            if mtime > now - 86400 and 'subagents' not in path.parts:
                                paths.append((mtime, path, agent))
                        except OSError:
                            continue
            for _, path, agent in sorted(paths, reverse=True)[:80]:
                if path not in self.tails:
                    self.tails[path] = Tail(path, agent)
            self.last_scan = now
        for path, tail in list(self.tails.items()):
            try:
                if path.stat().st_mtime < now - 86400:
                    del self.tails[path]
                    continue
                tail.poll(db)
            except OSError:
                continue
        self.codex_names.poll(db)
        self.process_monitor.poll(db)
