"""Small local event store. Never store prompts, tool arguments, or replies."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

PRIORITY = {'waiting': 0, 'error': 1, 'working': 2, 'done': 3, 'unknown': 4, 'idle': 5}


def connect(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'state.sqlite3', timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS sessions (
        agent TEXT NOT NULL, sid TEXT NOT NULL, project TEXT NOT NULL,
        state TEXT NOT NULL, event TEXT NOT NULL, origin TEXT NOT NULL,
        updated REAL NOT NULL, started REAL NOT NULL, turn_id TEXT NOT NULL,
        pending INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(agent,sid))''')
    return db


def record(db, *, agent, sid, event, cwd='', turn_id='', at=None, origin='hook', tool=''):
    if agent not in ('codex', 'claude') or not sid:
        return False
    at = time.time() if at is None else at
    sid, turn_id = str(sid)[:180], str(turn_id or '')[:180]
    with db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM sessions WHERE agent=? AND sid=?', (agent, sid)).fetchone()
        if old and at < old['updated']:
            return False
        starts = event in ('UserPromptSubmit', 'task_started', 'user_message')
        # A late Stop/PostToolUse from an earlier turn must not end the new turn.
        if old and old['turn_id'] and turn_id and old['turn_id'] != turn_id and not starts:
            return False
        pending = old['pending'] if old else 0
        status = old['state'] if old else 'idle'
        started = old['started'] if old else at
        if starts:
            if not old or old['turn_id'] != turn_id or old['state'] != 'working':
                started = at
            status, pending = 'working', 0
        elif event in ('PermissionRequest', 'permission_prompt', 'elicitation_dialog', 'elicitation_url_dialog'):
            status, pending = 'waiting', 2
        elif event in ('ask_async', 'ask_sync'):
            status, pending = 'waiting', 1 if event == 'ask_async' else 2
        elif event in ('PreToolUse', 'PostToolUse', 'activity', 'PreCompact', 'PostCompact'):
            # Async questions remain visible even while the agent continues other work.
            if pending == 1:
                status = 'waiting'
            else:
                status, pending = 'working', 0
            if tool.endswith('request_user_input_async'):
                status, pending = 'waiting', 1
            elif event == 'PreToolUse' and tool.rsplit('.', 1)[-1] in ('request_user_input', 'AskUserQuestion'):
                status, pending = 'waiting', 2
        elif event in ('Stop', 'task_complete'):
            status = 'waiting' if pending == 1 else 'done'
            if pending != 1:
                pending = 0
        elif event in ('PostToolUseFailure', 'StopFailure'):
            status = 'error'
        elif event in ('Interrupt', 'SessionEnd', 'turn_aborted'):
            status, pending = 'idle', 0
        elif event == 'SessionStart':
            status = old['state'] if old else 'idle'
        else:
            return False
        project = Path(str(cwd).replace('\\', '/')).name if cwd else (old['project'] if old else '未命名项目')
        db.execute('''INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(agent,sid) DO UPDATE SET
          project=excluded.project,state=excluded.state,event=excluded.event,
          origin=excluded.origin,updated=excluded.updated,started=excluded.started,
          turn_id=excluded.turn_id,pending=excluded.pending''',
          (agent, sid, project[:120], status, event, origin, at, started,
           turn_id or (old['turn_id'] if old else ''), pending))
    return True


def hook_event(db, agent, payload, event=None):
    name = event or payload.get('hook_event_name', '')
    if name == 'Notification':
        name = payload.get('notification_type', '')
    return record(db, agent=agent, sid=payload.get('session_id', ''), event=name,
                  cwd=payload.get('cwd', ''), turn_id=payload.get('turn_id', ''),
                  tool=payload.get('tool_name', ''))


def snapshot(db, now=None):
    now = time.time() if now is None else now
    rows = []
    for row in db.execute('SELECT * FROM sessions WHERE updated > ?', (now - 86400,)):
        if row['event'] == 'SessionEnd':
            continue
        item = dict(row)
        age = max(0, now - item['updated'])
        item['age'] = int(age)
        item['elapsed'] = int(max(0, now - item['started']))
        if item['state'] == 'done' and age > 600:
            continue
        elif item['state'] == 'idle' and age > 60:
            continue
        elif item['state'] in ('working', 'error') and age > 600:
            item['state'] = 'unknown'
        elif item['state'] == 'waiting' and age > 3600:
            item['state'] = 'unknown'
        rows.append(item)
    # Stable lanes: two busy agents should not swap rows on every tool call.
    rows.sort(key=lambda r: (PRIORITY[r['state']], r['project'].casefold(), r['agent'], r['sid']))
    state = rows[0]['state'] if rows else 'idle'
    return {'state': state, 'sessions': rows, 'updated': now}


def write_json(path: Path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
