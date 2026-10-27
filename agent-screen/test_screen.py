import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ds339 import WIDTH, HEIGHT, packet, parse_edid
from configure_hooks import codex_text, claude_text, install
from state import _bind_owner, bind_owner, connect, hook_event, record, snapshot, reconcile_owners
from process_owner import discover_claude_owners, discover_codex_owners, process_birth, process_matches
from render import demo_data, render
from watch import Tail, Watcher

ARTIFACTS = Path(__file__).resolve().parents[1] / '.local' / 'agent-screen-tests'
ARTIFACTS.mkdir(parents=True, exist_ok=True)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=ARTIFACTS))
        self.db = connect(self.root)

    def tearDown(self):
        self.db.close()

    def event(self, event, **kwargs):
        return record(self.db, agent='codex', sid='one', event=event, cwd='C:/work/project', **kwargs)

    def test_waiting_session_wins_over_another_completion(self):
        self.event('PermissionRequest', at=100)
        record(self.db, agent='claude', sid='two', event='Stop', at=101)
        result = snapshot(self.db, now=102)
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(len(result['sessions']), 2)

    def test_no_activity_never_means_done(self):
        self.event('UserPromptSubmit', at=100)
        self.assertEqual(snapshot(self.db, now=701)['state'], 'unknown')

    def test_late_stop_cannot_end_a_new_turn(self):
        self.event('UserPromptSubmit', turn_id='old', at=100)
        self.event('UserPromptSubmit', turn_id='new', at=105)
        self.event('Stop', turn_id='old', at=110)
        self.assertEqual(snapshot(self.db, now=111)['state'], 'working')

    def test_async_question_stays_red_during_other_work(self):
        self.event('UserPromptSubmit', at=100)
        self.event('PreToolUse', tool='request_user_input_async', at=101)
        self.event('PostToolUse', tool='Bash', at=102)
        self.event('Stop', at=103)
        self.assertEqual(snapshot(self.db, now=104)['state'], 'waiting')
        self.event('user_message', at=105)
        self.assertEqual(snapshot(self.db, now=106)['state'], 'working')

    def test_permission_resumes_and_interrupt_is_not_success(self):
        self.event('PermissionRequest', at=100)
        self.event('PostToolUse', at=101)
        self.assertEqual(snapshot(self.db, now=102)['state'], 'working')
        self.event('Interrupt', at=103)
        self.assertEqual(snapshot(self.db, now=104)['state'], 'idle')

    def test_old_log_event_cannot_overwrite_live_hook(self):
        self.event('UserPromptSubmit', at=100)
        self.event('Stop', at=90, origin='local-session')
        self.assertEqual(snapshot(self.db, now=101)['state'], 'working')

    def test_explicitly_closed_sessions_do_not_take_display_slots(self):
        self.event('SessionStart', at=100)
        self.event('SessionEnd', at=101)
        self.assertEqual(snapshot(self.db, now=102)['sessions'], [])

    def test_session_end_is_not_blocked_by_an_old_turn_id(self):
        self.event('UserPromptSubmit', turn_id='new', at=100)
        self.event('SessionEnd', turn_id='old', at=101)
        self.assertEqual(snapshot(self.db, now=102)['sessions'], [])

    def test_live_owner_keeps_completion_until_the_process_exits(self):
        self.event('Stop', at=100, owner=(123, 456))
        self.assertEqual(snapshot(self.db, now=1000)['state'], 'done')
        reconcile_owners(self.db, lambda pid, birth: False, now=1001)
        self.assertEqual(snapshot(self.db, now=1002)['sessions'], [])

    def test_unknown_process_access_does_not_mean_exit(self):
        self.event('Stop', at=100, owner=(123, 456))
        reconcile_owners(self.db, lambda pid, birth: None, now=101)
        self.assertEqual(snapshot(self.db, now=102)['state'], 'done')

    def test_buffered_completion_does_not_revive_an_exited_process(self):
        self.event('Stop', at=100, owner=(123, 456))
        reconcile_owners(self.db, lambda pid, birth: False, now=101)
        self.event('Stop', at=102, origin='local-session')
        self.event('PostToolUse', at=103, owner=(123, 456))
        self.assertEqual(snapshot(self.db, now=104)['sessions'], [])
        self.event('UserPromptSubmit', at=105, owner=(789, 1000))
        self.assertEqual(snapshot(self.db, now=106)['state'], 'working')

    def test_reused_pid_does_not_close_a_new_owner(self):
        self.event('Stop', at=100, owner=(123, 456))
        self.event('UserPromptSubmit', at=101, owner=(123, 789))
        reconcile_owners(self.db, lambda pid, birth: birth == 789, now=102)
        self.assertEqual(snapshot(self.db, now=103)['state'], 'working')

    def test_one_owner_exit_does_not_close_another_live_owner(self):
        self.event('UserPromptSubmit', at=100, owner=(123, 456))
        self.event('Stop', at=101, owner=(789, 1000))
        reconcile_owners(self.db, lambda pid, birth: pid == 789, now=102)
        self.assertEqual(snapshot(self.db, now=103)['state'], 'done')

    def test_conversation_reopened_in_a_new_process_shows_as_idle(self):
        self.event('Stop', at=100, owner=(123, 456))
        reconcile_owners(self.db, lambda pid, birth: False, now=101)  # e.g. Windows restarted
        self.assertEqual(snapshot(self.db, now=102)['sessions'], [])
        bind_owner(self.db, 'codex', 'one', (789, 1000), source='open-transcript')
        rows = snapshot(self.db)['sessions']
        self.assertEqual([(r['sid'], r['state'], r['process_bound']) for r in rows], [('one', 'idle', True)])

    def test_live_owner_bound_earlier_still_revives_an_exited_conversation(self):
        self.event('Stop', at=100, owner=(123, 456))
        reconcile_owners(self.db, lambda pid, birth: False, now=101)
        with self.db:  # bound by an earlier version that did not revive
            _bind_owner(self.db, 'codex', 'one', (789, 1000), 102, 'open-transcript')
        self.assertEqual(snapshot(self.db, now=103)['sessions'], [])
        bind_owner(self.db, 'codex', 'one', (789, 1000), source='open-transcript')
        self.assertEqual(snapshot(self.db)['sessions'][0]['state'], 'idle')

    def test_same_process_never_revives_a_conversation_that_ended_in_it(self):
        self.event('UserPromptSubmit', at=100, owner=(123, 456))
        self.event('SessionEnd', at=101, owner=(123, 456))
        bind_owner(self.db, 'codex', 'one', (123, 456), source='open-transcript')
        self.assertEqual(snapshot(self.db)['sessions'], [])

    def test_unknown_conversation_with_a_live_process_is_listed(self):
        bind_owner(self.db, 'claude', 'new', (5, 6), source='session-registry', project='notes')
        row = snapshot(self.db)['sessions'][0]
        self.assertEqual((row['agent'], row['project'], row['state']), ('claude', 'notes', 'idle'))

    @unittest.skipUnless(os.name == 'nt', 'Native Windows process identity test')
    def test_claude_registry_lists_live_interactive_sessions_only(self):
        folder = self.root / '.claude' / 'sessions'
        folder.mkdir(parents=True)
        me, birth = os.getpid(), process_birth(os.getpid())
        entries = {'live': {'pid': me, 'sessionId': 'abc', 'procStart': str(birth), 'kind': 'interactive',
                            'cwd': 'C:/work/notes'},
                   'stale': {'pid': me, 'sessionId': 'old', 'procStart': '42', 'kind': 'interactive'},
                   'print': {'pid': me, 'sessionId': 'p', 'procStart': str(birth), 'kind': 'sdk-cli'}}
        for name, info in entries.items():
            (folder / (name + '.json')).write_text(json.dumps(info), encoding='utf-8')
        self.assertEqual(discover_claude_owners(self.root), [('claude', 'abc', (me, birth), 'notes')])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows process identity test')
    def test_running_out_of_memory_while_listing_codex_files_is_skipped(self):
        class Codex:
            pid, info = os.getpid(), {'pid': os.getpid(), 'name': 'codex.exe'}
            def cmdline(self): return ['codex.exe']
            def open_files(self): raise MemoryError
        with mock.patch('psutil.process_iter', return_value=[Codex()]):
            self.assertEqual(discover_codex_owners(), [])

    @unittest.skipUnless(os.name == 'nt', 'Native Windows process identity test')
    def test_real_process_exit_clears_completed_session_without_an_end_hook(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            birth = process_birth(child.pid)
            self.assertTrue(birth)
            self.event('Stop', at=100, owner=(child.pid, birth))
            watcher = Watcher(home=self.root)
            watcher.process_monitor.last_discovery = time.time()
            watcher.poll(self.db)
            self.assertEqual(snapshot(self.db, now=102)['state'], 'done')
            child.stdin.close()
            child.wait(timeout=5)
            watcher.poll(self.db)
            self.assertEqual(snapshot(self.db, now=104)['sessions'], [])
        finally:
            if child.poll() is None:
                child.stdin.close()
                child.wait(timeout=5)

    def test_old_completion_leaves_the_display_instead_of_becoming_idle(self):
        self.event('Stop', at=100)
        self.assertEqual(snapshot(self.db, now=699)['state'], 'done')
        self.assertEqual(snapshot(self.db, now=701)['sessions'], [])

    def test_old_idle_entry_does_not_look_like_an_open_task(self):
        self.event('SessionStart', at=100)
        self.assertEqual(len(snapshot(self.db, now=110)['sessions']), 1)
        self.assertEqual(snapshot(self.db, now=161)['sessions'], [])

    def test_attention_and_work_are_visible_before_idle_overflow(self):
        for i in range(9):
            record(self.db, agent='codex', sid=str(i), event='SessionStart', cwd=f'C:/a/idle-{i}', at=100)
        record(self.db, agent='codex', sid='running', event='UserPromptSubmit', cwd='C:/z/work', at=101)
        record(self.db, agent='codex', sid='waiting', event='PermissionRequest', cwd='C:/z/wait', at=101)
        rows = snapshot(self.db, now=102)['sessions']
        self.assertEqual([r['sid'] for r in rows[:2]], ['waiting', 'running'])
        self.assertIn('running', [r['sid'] for r in rows[:6]])

    def test_hooks_do_not_store_content_or_complete_parent_for_subagent(self):
        hook_event(self.db, 'claude', {'session_id': 'test', 'hook_event_name': 'UserPromptSubmit',
                                     'cwd': 'C:/lab/实验', 'prompt': 'PRIVATE_PROMPT'})
        hook_event(self.db, 'claude', {'session_id': 'test', 'hook_event_name': 'SubagentStop'})
        row = snapshot(self.db)['sessions'][0]
        self.assertEqual(row['state'], 'working')
        self.assertEqual(row['project'], '实验')
        self.assertNotIn('PRIVATE_PROMPT', json.dumps(row))

    def test_failed_tool_call_from_background_helper_is_not_an_error(self):
        # Seen in plan mode: the parent turn ended, a helper's search exited 1 and the helper carried on.
        hook_event(self.db, 'claude', {'session_id': 'plan', 'hook_event_name': 'Stop', 'cwd': 'C:/work/plan'})
        hook_event(self.db, 'claude', {'session_id': 'plan', 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash'})
        hook_event(self.db, 'claude', {'session_id': 'plan', 'hook_event_name': 'PostToolUseFailure',
                                     'tool_name': 'Bash', 'error': 'Exit code 1', 'is_interrupt': False})
        self.assertEqual(snapshot(self.db)['state'], 'working')

    def test_aborted_turn_stays_red_until_the_next_prompt(self):
        # Claude Code sends StopFailure (no Stop) when an API error ends the turn.
        record(self.db, agent='claude', sid='two', event='UserPromptSubmit', at=100)
        record(self.db, agent='claude', sid='two', event='StopFailure', at=101)
        self.assertEqual(snapshot(self.db, now=1000)['state'], 'error')
        self.assertEqual(snapshot(self.db, now=3702)['state'], 'unknown')
        record(self.db, agent='claude', sid='two', event='UserPromptSubmit', at=3800)
        self.assertEqual(snapshot(self.db, now=3801)['state'], 'working')

    def test_partial_json_line_and_explicit_completion(self):
        path = self.root / 'rollout.jsonl'
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + 'Z'
        meta = {'type': 'session_meta', 'payload': {'id': 'tail', 'cwd': 'C:/work/tail'}}
        start = {'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't'}}
        done = {'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_complete'}}
        encoded = json.dumps(done)
        path.write_text(json.dumps(meta) + '\n' + json.dumps(start) + '\n' + encoded[:20], encoding='utf-8')
        tail = Tail(path, 'codex')
        tail.poll(self.db)
        self.assertEqual(snapshot(self.db)['state'], 'working')
        with path.open('a', encoding='utf-8') as f:
            f.write(encoded[20:] + '\n')
        tail.poll(self.db)
        self.assertEqual(snapshot(self.db)['state'], 'done')

    def test_claude_rename_beats_automatic_titles_and_untitled_keeps_folder(self):
        path = self.root / 'claude.jsonl'
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + 'Z'
        lines = [{'type': 'user', 'sessionId': 'chat', 'cwd': 'C:/work/folder', 'timestamp': stamp,
                  'message': {'role': 'user', 'content': 'hi'}},
                 {'type': 'ai-title', 'aiTitle': '自动标题', 'sessionId': 'chat'},
                 {'type': 'custom-title', 'customTitle': '我的名字', 'sessionId': 'chat'},
                 {'type': 'ai-title', 'aiTitle': '新的自动标题', 'sessionId': 'chat'}]
        path.write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in lines), encoding='utf-8')
        Tail(path, 'claude').poll(self.db)
        self.event('UserPromptSubmit')
        rows = {r['sid']: r for r in snapshot(self.db)['sessions']}
        self.assertEqual(rows['chat']['title'], '我的名字')
        self.assertEqual((rows['one']['title'], rows['one']['project']), ('', 'project'))

    def test_codex_thread_name_follows_the_last_index_entry(self):
        index = self.root / '.codex' / 'session_index.jsonl'
        index.parent.mkdir()
        names = [{'id': 'one', 'thread_name': '起草方案'}, {'id': 'one', 'thread_name': '数据清洗'}]
        index.write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in names), encoding='utf-8')
        self.event('UserPromptSubmit')
        watcher = Watcher(home=self.root)
        watcher.process_monitor.last_discovery = time.time()
        watcher.poll(self.db)
        self.assertEqual(snapshot(self.db)['sessions'][0]['title'], '数据清洗')
        with index.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'id': 'one', 'thread_name': '改过的名字'}, ensure_ascii=False) + '\n')
        watcher.poll(self.db)
        self.assertEqual(snapshot(self.db)['sessions'][0]['title'], '改过的名字')


class TransportTests(unittest.TestCase):
    def test_frame_matches_captured_native_geometry(self):
        frame = packet(bytes(WIDTH * HEIGHT * 3))
        self.assertEqual(len(frame), 1082896)
        self.assertEqual(frame[:8], bytes.fromhex('ff000000001783c0'))
        self.assertEqual(frame[-8:], bytes.fromhex('ffc0000000000000'))
        with self.assertRaises(ValueError):
            packet(b'wrong resolution')

    def test_edid_is_checked_before_accepting_a_panel(self):
        edid = bytearray(128)
        edid[:8] = bytes.fromhex('00ffffffffffff00')
        edid[56], edid[58] = 0x78, 0x10
        edid[59], edid[61] = 0xC0, 0x30
        edid[127] = (-sum(edid)) % 256
        self.assertEqual(parse_edid(edid), (376, 960))
        edid[20] ^= 1
        with self.assertRaises(RuntimeError):
            parse_edid(edid)


class RenderTests(unittest.TestCase):
    def test_locked_frame_draws_no_title_and_keeps_lamps_and_states(self):
        data = demo_data()
        renamed = {**data, 'sessions': [{**s, 'title': '另一个名字'} for s in data['sessions']]}
        self.assertNotEqual(render(data).tobytes(), render(renamed).tobytes())
        hidden = render({**data, 'private': True})
        self.assertEqual(hidden.tobytes(), render({**renamed, 'private': True}).tobytes())
        for box in ((0, 0, 400, 376), (800, 0, 960, 376)):
            self.assertEqual(hidden.crop(box).tobytes(), render(data).crop(box).tobytes())


class ConfigTests(unittest.TestCase):
    def test_codex_append_and_remove_preserve_existing_hook(self):
        import tomllib
        original = 'model="original"\n[[hooks.PreToolUse]]\n[[hooks.PreToolUse.hooks]]\ntype="command"\ncommand="existing-auth-hook"\n'
        installed = codex_text(original, 'python "screen.py" hook --agent codex')
        second = codex_text(installed, 'python "screen.py" hook --agent codex')
        self.assertEqual(installed, second)
        parsed = tomllib.loads(second)
        self.assertEqual(parsed['model'], 'original')
        self.assertEqual(len(parsed['hooks']['PreToolUse']), 2)
        removed = codex_text(second, '', remove=True)
        self.assertEqual(tomllib.loads(removed), tomllib.loads(original))

    def test_codex_rebuild_keeps_trust_saved_inside_the_block(self):
        import tomllib
        cmd = 'python "screen.py" hook --agent codex'
        installed = codex_text('model="original"\n', cmd)
        # Seen on the host: /hooks review saved its trust tables between two of this block's hooks.
        trust = "[hooks.state]\n\n[hooks.state.'config.toml:stop:0:0']\ntrusted_hash = \"sha256:abc\"\n\n"
        at = installed.index('[[hooks.PostToolUse]]')
        live = installed[:at] + trust + installed[at:]
        rebuilt = codex_text(live, cmd)
        self.assertEqual(tomllib.loads(rebuilt), tomllib.loads(live))
        self.assertEqual(rebuilt, codex_text(rebuilt, cmd))
        removed = tomllib.loads(codex_text(live, '', remove=True))
        self.assertEqual(removed['hooks'], {'state': tomllib.loads(live)['hooks']['state']})

    def test_install_leaves_equivalent_files_untouched(self):
        home = Path(tempfile.mkdtemp(dir=ARTIFACTS))
        self.assertTrue(all(r['applied'] for r in install(home, home / 'data', apply=True)))
        again = install(home, home / 'data', apply=True)
        self.assertEqual([(r['applied'], r['backup']) for r in again], [(False, None), (False, None)])

    def test_claude_preserves_unrelated_settings_and_handlers(self):
        original = {'model': 'unchanged', 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'keep-me'}]}]}}
        installed = claude_text(json.dumps(original), 'screen-command')
        self.assertEqual(installed, claude_text(installed, 'screen-command'))
        self.assertEqual(json.loads(installed)['model'], 'unchanged')
        removed = json.loads(claude_text(installed, 'screen-command', remove=True))
        self.assertEqual(removed, original)

    def test_claude_registers_aborted_turns(self):
        installed = json.loads(claude_text('{}', 'screen-command'))
        self.assertEqual(installed['hooks']['StopFailure'],
                         [{'hooks': [{'type': 'command', 'command': 'screen-command', 'timeout': 2}]}])


if __name__ == '__main__':
    unittest.main()
