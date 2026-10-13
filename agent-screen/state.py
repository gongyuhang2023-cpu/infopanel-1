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
    db.execute('''CREATE TABLE IF NOT EXISTS session_owners (
        agent TEXT NOT NULL, sid TEXT NOT NULL, pid INTEGER NOT NULL,
        birth INTEGER NOT NULL, observed REAL NOT NULL, source TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(agent,sid,pid,birth))''')
    return db


def _bind_owner(db, agent, sid, owner, at, source):
    pid, birth = owner
    if pid > 0 and birth > 0:
        db.execute('''INSERT INTO session_owners VALUES (?,?,?,?,?,?,0)
            ON CONFLICT(agent,sid,pid,birth) DO UPDATE SET
            observed=excluded.observed,source=excluded.source,closed=0''',
            (agent, sid, pid, birth, at, source))


def bind_owner(db, agent, sid, owner, source='hook'):
    with db:
        db.execute('BEGIN IMMEDIATE')
        _bind_owner(db, agent, sid, owner, time.time(), source)


def reconcile_owners(db, is_alive, now=None):
    """Close a session only after all positively identified owners have exited."""
    now = time.time() if now is None else now
    owners = db.execute('SELECT DISTINCT pid,birth FROM session_owners WHERE closed=0').fetchall()
    dead = [(r['pid'], r['birth']) for r in owners if is_alive(r['pid'], r['birth']) is False]
    if not dead:
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        affected = set()
        for pid, birth in dead:
            affected.update(tuple(r) for r in db.execute(
                'SELECT agent,sid FROM session_owners WHERE pid=? AND birth=? AND closed=0', (pid, birth)))
            db.execute('UPDATE session_owners SET closed=1 WHERE pid=? AND birth=?', (pid, birth))
        for agent, sid in affected:
            other = db.execute('SELECT 1 FROM session_owners WHERE agent=? AND sid=? AND closed=0 LIMIT 1',
                               (agent, sid)).fetchone()
            if other is None:
                db.execute('''UPDATE sessions SET state='idle', event='ProcessExit',
                    origin='process-exit',updated=?,pending=0
                    WHERE agent=? AND sid=? AND updated<=?
                    AND event NOT IN ('SessionEnd','ProcessExit')''', (now, agent, sid, now))


def record(db, *, agent, sid, event, cwd='', turn_id='', at=None, origin='hook', tool='', owner=None):
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
        if old and old['event'] in ('SessionEnd', 'ProcessExit') and not starts and event != 'SessionStart':
            # Buffered log output or an orphan hook must not revive a closed session.
            if owner is None:
                return False
            previous_owner = db.execute('''SELECT closed FROM session_owners
                WHERE agent=? AND sid=? AND pid=? AND birth=?''', (agent, sid, *owner)).fetchone()
            if previous_owner and previous_owner['closed']:
                return False
        # A late Stop/PostToolUse from an earlier turn must not end the new turn.
        if (old and old['turn_id'] and turn_id and old['turn_id'] != turn_id
                and not starts and event not in ('SessionStart', 'SessionEnd')):
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
        elif event in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'activity', 'PreCompact', 'PostCompact'):
            # Async questions remain visible even while the agent continues other work.
            # A failed tool call (non-zero exit, search timeout) is routine; the agent carries on.
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
        elif event == 'StopFailure':
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
        if owner is not None and event != 'SessionEnd':
            _bind_owner(db, agent, sid, owner, at, 'hook')
        if event == 'SessionEnd':
            db.execute('UPDATE session_owners SET closed=1 WHERE agent=? AND sid=?', (agent, sid))
    return True


def hook_event(db, agent, payload, event=None):
    from process_owner import current_owner
    name = event or payload.get('hook_event_name', '')
    if name == 'Notification':
        name = payload.get('notification_type', '')
    return record(db, agent=agent, sid=payload.get('session_id', ''), event=name,
                  cwd=payload.get('cwd', ''), turn_id=payload.get('turn_id', ''),
                  tool=payload.get('tool_name', ''), owner=current_owner(agent))


def snapshot(db, now=None):
    now = time.time() if now is None else now
    rows = []
    live = {(r['agent'], r['sid']) for r in db.execute('SELECT agent,sid FROM session_owners WHERE closed=0')}
    for row in db.execute('''SELECT s.* FROM sessions s WHERE updated > ? OR EXISTS
        (SELECT 1 FROM session_owners o WHERE o.agent=s.agent AND o.sid=s.sid AND o.closed=0)''', (now - 86400,)):
        if row['event'] in ('SessionEnd', 'ProcessExit'):
            continue
        item = dict(row)
        age = max(0, now - item['updated'])
        item['age'] = int(age)
        item['elapsed'] = int(max(0, now - item['started']))
        item['process_bound'] = (item['agent'], item['sid']) in live
        if item['state'] == 'done' and age > 600 and not item['process_bound']:
            continue
        elif item['state'] == 'idle' and age > 60 and not item['process_bound']:
            continue
        elif item['state'] == 'working' and age > 600:
            item['state'] = 'unknown'
        elif item['state'] in ('waiting', 'error') and age > 3600:
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
