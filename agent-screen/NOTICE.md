# Source and licenses

This standalone `agent-screen/` component is GPL-3.0, see LICENSE. This does not
change the license of unrelated files in the parent repository.

`ds339.py` adapts the DS339 protocol, initialization sequence, and frame format
from InfoPanel's `JonsboMs9132Device.cs`, by the InfoPanel contributors:

- https://github.com/emaspa/infopanel-1
- User fork (created 2026-10-09): https://github.com/gongyuhang2023-cpu/infopanel-1
- Commit: `3ae25f035ac3631c0ee8a5bd41e6269c81c29814`
- File: `InfoPanel/JonsboPanel/JonsboMs9132Device.cs`
- Original license: GPL-3.0. Python adaptation and device-selection safeguards
  added for this project on 2026-10-09.

The signal-screen layout and agent event store are developed in this repository's
`agent-screen/` directory. They are a Python derivative, not the original InfoPanel
WPF interface. The fork preserves the upstream C# source and history separately.

The installed JONSBO/libusb Windows driver is used in place. No manufacturer DLL,
driver installer, JonsboCanvas code, credentials, or firmware is distributed here.

Agent integration references (no copied implementation):
- https://learn.chatgpt.com/docs/hooks
- https://code.claude.com/docs/en/hooks
- https://github.com/dpf202507/Codex-Light (software MIT)
- https://github.com/peters385/codex-traffic-light-windows (MIT)

Python dependencies retain their own licenses: Pillow (HPND), PyUSB (BSD-3-Clause),
hidapi (BSD/GPL alternatives), psutil (BSD-3-Clause). Consult the installed package
metadata for full license texts.
