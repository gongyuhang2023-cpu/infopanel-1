"""DS339 transport using the existing JONSBO libusb0 driver.

GPL-3.0. Adapted from InfoPanel; attribution and pinned source are in NOTICE.md.
Only the confirmed 376x960 DS339 timing is accepted. No firmware/flash writes.
"""
from __future__ import annotations

import os
import struct
import time

VID, PID = 0x345F, 0x9132
WIDTH, HEIGHT = 376, 960
CHUNK = 65536

# Volatile power/register setup from the upstream OEM capture.
STARTUP = [
    [0xB5, 0xFF, 0], [0xB5, 0xF0, 0], [0xB5, 0, 0x31],
    [0xB5, 0, 0x30], [0xB5, 0xF4, 0x39],
    [0xF5, 0, 0x1F, 0xE0], [0xF5, 0, 0x1F, 0xE8],
    [0xF5, 0, 0x1F, 0xF0], [0xF5, 0, 0x1F, 0xF8],
    [0xA6, 7, 1, 2], [0xB5, 0xC4, 0x54], [0xA6, 5, 0],
    [0xB5, 0xC5, 0x55], [0xB5, 0xF0, 5], [0xB6, 0xF0, 5, 0x20],
    [0xC5, 0xB0, 0], [0xC6, 0xB0, 0xDF], [0xC5, 0xA0, 0],
    [0xC6, 0xA0, 0xE4], [0xC5, 0xA0, 0], [0xC6, 0xA0, 0xF0],
    [0xB5, 0xF0, 0x16], [0xB6, 0xF0, 0x16, 0], [0xB5, 0, 0x32],
]


def packet(bgr: bytes) -> bytes:
    expected = WIDTH * HEIGHT * 3
    if len(bgr) != expected:
        raise ValueError(f"Expected {expected} BGR bytes, got {len(bgr)}")
    header = bytes([0xFF, 0, 0, 0, 0, WIDTH // 16]) + struct.pack('>H', HEIGHT | 0x8000)
    return header + bgr + bytes([0xFF, 0xC0, 0, 0, 0, 0, 0, 0])


def parse_edid(edid: bytes) -> tuple[int, int]:
    if len(edid) != 128 or edid[:8] != b'\x00\xff\xff\xff\xff\xff\xff\x00':
        raise RuntimeError('Device did not return a valid panel EDID')
    if sum(edid) % 256:
        raise RuntimeError('Panel EDID checksum failed; refusing mode setup')
    w = ((edid[58] & 0xF0) << 4) | edid[56]
    h = ((edid[61] & 0xF0) << 4) | edid[59]
    return w, h


def backend():
    import usb.backend.libusb0
    dll = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'libusb0.dll')
    result = usb.backend.libusb0.get_backend(find_library=lambda _: dll)
    if result is None:
        raise RuntimeError('JONSBO x64 libusb0 driver library is not available')
    return result


def inventory() -> dict:
    import hid
    import usb.core
    devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=backend()))
    return {'usb_devices': len(devices), 'hid_interfaces': len(hid.enumerate(VID, PID)),
            'vid_pid': '345F:9132', 'driver': 'existing libusb0'}


class DS339:
    def __init__(self):
        self.hid = None
        self.usb = None
        self.claimed = False
        self.enabled = False
        self.initialized = False
        self.frames = 0

    def report(self, payload: list[int]) -> bytes:
        buf = bytes([0] + payload + [0] * (8 - len(payload)))
        self.hid.send_feature_report(buf)
        reply = bytes(self.hid.get_feature_report(0, 9))
        if len(reply) != 9:
            raise OSError('Short HID feature reply')
        op, reg = payload[:2]
        delay = {(0xA6, 7): .105, (0xA6, 5): .015, (0xA6, 3): .015,
                 (0xA6, 1): .015, (0xA6, 2): .025}.get((op, reg), .003)
        if payload[:3] == [0xB5, 0, 0x32]:
            delay = .108
        time.sleep(delay)
        return reply[4:9]

    def open(self):
        import hid
        import usb.core
        import usb.util
        devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=backend()))
        hids = [d for d in hid.enumerate(VID, PID) if d['interface_number'] == 0]
        if len(devices) != 1 or len(hids) != 1:
            raise RuntimeError(f'Expected exactly one DS339, found USB={len(devices)}, HID={len(hids)}')
        self.usb = devices[0]
        try:
            # Claim only the display interface; never change the composite configuration.
            usb.util.claim_interface(self.usb, 3)
            self.claimed = True
            self.hid = hid.device()
            self.hid.open_path(hids[0]['path'])
            # Identity check before power/mode writes. Same VID/PID can cover other panels.
            edid = b''.join(self.report([0xB5, 0xC0, i])[:4] for i in range(0, 128, 4))
            size = parse_edid(edid)
            if size != (WIDTH, HEIGHT):
                raise RuntimeError(f'Panel reports {size}; this program only supports DS339 376x960')
            for command in STARTUP:
                self.report(command)
            for i in range(0, 128, 4):
                self.report([0xB5, 0xC0, i])
            commands = [
                [0xB5, 0, 0x32], [0xB5, 0xF4, 0x39], [0xA6, 3, 3],
                [0xB5, 0xC5, 0x58], [0xA6, 1, 1, 0x78, 3, 0xC0, 0x11, 0],
                [0xB5, 0xC5, 0x56], [0xA6, 2, 171, 0, 1, 0x78, 3, 0xC0],
                [0xB5, 0xC5, 0x57], [0xA6, 4, 1],
            ]
            for command in commands:
                self.report(command)
            time.sleep(.4)
            self.initialized = True
            return self
        except Exception:
            self.close()
            raise

    def send(self, image, rotation=90):
        from PIL import Image
        if image.size != (HEIGHT, WIDTH):
            raise ValueError('Logical screen canvas must be 960x376')
        if rotation not in (90, 270):
            raise ValueError('Rotation must be 90 or 270')
        native = image.transpose(Image.Transpose.ROTATE_90 if rotation == 90 else Image.Transpose.ROTATE_270)
        data = packet(native.convert('RGB').tobytes('raw', 'BGR'))
        for offset in range(0, len(data), CHUNK):
            chunk = data[offset:offset + CHUNK]
            written = self.usb.write(0x04, chunk, timeout=2000)
            if written != len(chunk):
                raise OSError(f'Short USB frame write at {offset}: {written}/{len(chunk)}')
        self.frames += 1
        if not self.enabled:
            for command in ([0xB5, 0xF0, 5], [0xB6, 0xF0, 5, 0x30],
                            [0xA6, 5, 1], [0xB5, 0xC5, 0x55]):
                self.report(command)
            self.enabled = True

    def close(self, power_off=False):
        import usb.util
        if power_off and self.initialized and self.hid:
            try:
                self.report([0xA6, 7, 0])
            except OSError:
                pass
        if self.hid:
            self.hid.close()
            self.hid = None
        if self.usb is not None:
            if self.claimed:
                usb.util.release_interface(self.usb, 3)
            usb.util.dispose_resources(self.usb)
            self.usb = None
        self.claimed = False
