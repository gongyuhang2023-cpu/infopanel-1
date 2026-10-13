"""Append Agent Screen hooks, preserving other hooks/settings and private backups."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path

BEGIN = '# BEGIN AGENT_SCREEN_HOOKS'
END = '# END AGENT_SCREEN_HOOKS'
CODEX_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
                'PermissionRequest', 'Stop', 'Interrupt', 'SessionEnd')
CLAUDE_EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
                 'PermissionRequest', 'PostToolUseFailure', 'Stop', 'SessionEnd', 'Notification')


def command(script, data_root, agent):
    # python is already used by this host's existing hooks. No optional packages
    # are imported on the hook path. Works in both cmd/PowerShell and Git Bash.
    values = [str(Path(p).resolve()).replace('\\', '/') for p in (script, data_root)]
    if any(any(c in s for c in '\n\r"%$`') for s in values):
        raise ValueError('Hook paths contain shell expansion characters')
    return f'python "{values[0]}" --data-root "{values[1]}" hook --agent {agent}'


def codex_text(original, cmd, remove=False):
    kept = []

    def strip(match):
        # Codex saves hook trust ([hooks.state...] tables) where it likes, including inside this block.
        for part in re.split(r'(?m)^(?=\[)', match.group(1)):
            if part.startswith('[') and not part.startswith('[[hooks.'):
                kept.append(part.rstrip() + '\n')
        return ''

    text = re.sub(r'(?m)^' + re.escape(BEGIN) + r'\r?\n(.*?)^' + re.escape(END) + r'[^\n]*\n?',
                  strip, original, flags=re.S)
    if kept:
        text = text.rstrip() + '\n\n' + '\n'.join(kept)
    if not remove:
        parts = [BEGIN]
        for event in CODEX_EVENTS:
            parts.extend([f'[[hooks.{event}]]', f'[[hooks.{event}.hooks]]',
                          'type = "command"', 'command = ' + json.dumps(cmd), 'timeout = 2', ''])
        parts.append(END)
        text = text.rstrip() + '\n\n' + '\n'.join(parts) + '\n'
    tomllib.loads(text)  # Validate before changing any file.
    return text


def claude_text(original, cmd, remove=False):
    data = json.loads(original or '{}')
    hooks = data.setdefault('hooks', {})
    for event in CLAUDE_EVENTS:
        groups = []
        for group in hooks.get(event, []):
            handlers = [h for h in group.get('hooks', []) if h.get('command') != cmd]
            if handlers:
                groups.append({**group, 'hooks': handlers})
        if not remove:
            group = {'hooks': [{'type': 'command', 'command': cmd, 'timeout': 2}]}
            if event == 'Notification':
                group['matcher'] = 'permission_prompt|elicitation_dialog|elicitation_url_dialog'
            groups.append(group)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def install(home, data_root, apply=False, remove=False):
    script = Path(__file__).with_name('screen.py')
    paths = [(home / '.codex' / 'config.toml', 'codex', codex_text, tomllib.loads),
             (home / '.claude' / 'settings.json', 'claude', claude_text, json.loads)]
    planned = []
    for path, agent, transform, parse in paths:
        original = path.read_text(encoding='utf-8-sig') if path.exists() else ''
        cmd = command(script, data_root, agent)
        updated = transform(original, cmd, remove)
        # An equivalent file is left alone rather than rewritten, e.g. with moved trust tables.
        changed = not original.strip() or parse(original) != parse(updated)
        planned.append((path, updated, changed))
    result = []
    for path, updated, changed in planned:
        backup = None
        if apply and changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                backups = path.parent / 'backups' / 'agent-screen'
                backups.mkdir(parents=True, exist_ok=True)
                backup = backups / (datetime.now().strftime('%Y%m%d-%H%M%S-%f-') + path.name)
                shutil.copy2(path, backup)
            tmp = path.with_name(path.name + '.agent-screen.tmp')
            tmp.write_text(updated, encoding='utf-8')
            tmp.replace(path)
        result.append({'path': str(path), 'applied': apply and changed, 'remove': remove,
                       'backup': str(backup) if backup else None})
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home', type=Path, default=Path.home())
    p.add_argument('--data-root', type=Path, default=Path(__file__).resolve().parents[1] / '.local' / 'agent-screen')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--remove', action='store_true')
    args = p.parse_args()
    print(json.dumps(install(args.home, args.data_root, args.apply, args.remove), indent=2))
