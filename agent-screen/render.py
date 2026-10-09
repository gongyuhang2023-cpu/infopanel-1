"""A legible 960x376 signal panel, rendered locally without browser/GPU capture."""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from state import PRIORITY

COLORS = {'waiting': '#FA635A', 'error': '#FA635A', 'working': '#F4C44E',
          'done': '#57D6A2', 'unknown': '#9AABC1', 'idle': '#657086'}
LABELS = {'waiting': '等你回应', 'error': '执行异常', 'working': '正在运行',
          'done': '本轮结束', 'unknown': '状态待确认', 'idle': '待命'}
SHORT = {'waiting': '等回应', 'error': '异常', 'working': '运行中',
         'done': '已结束', 'unknown': '待确认', 'idle': '空闲'}
BG, PANEL, WHITE, MUTED = '#101722', '#1B2533', '#F3F6FA', '#A5B3C5'


@lru_cache(maxsize=24)
def font(size, bold=False):
    base = Path('C:/Windows/Fonts')
    choices = ['msyhbd.ttc', 'msyh.ttc'] if bold else ['msyh.ttc', 'segoeui.ttf']
    for name in choices:
        if (base / name).exists():
            return ImageFont.truetype(str(base / name), size)
    return ImageFont.load_default(size=size)


def fitted(draw, value, size, width, bold=False):
    f = font(size, bold)
    if draw.textlength(value, font=f) <= width:
        return value
    while value and draw.textlength(value + '…', font=f) > width:
        value = value[:-1]
    return value + '…'


def render(data, demo=False):
    image = Image.new('RGB', (960, 376), BG)
    d = ImageDraw.Draw(image)
    status = data['state']
    accent = COLORS[status]
    sessions = data['sessions']
    if not sessions:
        d.text((480, 188), '无 AI 会话', font=font(48, True), fill=MUTED, anchor='mm')
        if demo:
            d.text((28, 352), '演示', font=font(18), fill=MUTED, anchor='lm')
        return image
    lamps = sessions[:6]
    columns = 1 if len(lamps) <= 1 else 2 if len(lamps) <= 4 else 3
    lamp_rows = 1 if len(lamps) <= 2 else 2
    cell_width, cell_height = 324 / columns, 244 / lamp_rows
    radius = (min(cell_width, cell_height) - 18) / 2
    # Each dot is now one session. Its number matches the detailed row on the right.
    for i, session in enumerate(lamps):
        row, col = divmod(i, columns)
        x = 180 + (col - (columns - 1) / 2) * cell_width
        y = 14 + (row + .5) * cell_height
        color = COLORS[session['state']]
        d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        if session['state'] in ('waiting', 'error', 'working'):
            r = radius + 5
            d.ellipse((x - r, y - r, x + r, y + r), outline=color, width=2)
        ink = BG if session['state'] in ('waiting', 'error', 'working', 'done') else WHITE
        d.text((x, y - 1), str(i + 1), font=font(max(27, int(radius * .8)), True), fill=ink, anchor='mm')

    label_size = 44 if len(LABELS[status]) <= 4 else 38
    d.text((180, 304), LABELS[status], font=font(label_size, True), fill=accent, anchor='mm')
    if len(sessions) > 6:
        d.text((180, 352), f'另有 {len(sessions) - 6} 个会话', font=font(19), fill=MUTED, anchor='mm')

    card_count = min(len(sessions), 4)
    card_height, gap = (78, 10) if card_count == 4 else (84, 14)
    top = (376 - (card_count * card_height + max(0, card_count - 1) * gap)) // 2
    for i, session in enumerate(sessions[:4]):
        y = top + i * (card_height + gap)
        color = COLORS[session['state']]
        d.rounded_rectangle((366, y, 942, y + card_height), radius=12, fill=PANEL)
        d.rounded_rectangle((366, y + 13, 371, y + card_height - 13), radius=2, fill=color)
        middle = y + card_height / 2
        d.text((391, middle), str(i + 1), font=font(24, True), fill=color, anchor='mm')
        title = fitted(d, session['project'], 30, 378, True)
        d.text((413, middle), title, font=font(30, True), fill=WHITE, anchor='lm')
        d.text((814, middle), SHORT[session['state']], font=font(26, True), fill=color, anchor='lm')
    if demo:
        d.rounded_rectangle((12, 344, 96, 372), radius=6, fill='#354155')
        d.text((54, 357), '演示', font=font(18), fill=WHITE, anchor='mm')
    return image


def demo_data(state='working', count=6):
    now = time.time()
    projects = ['screen-demo', 'api-service', 'docs-site', '分析任务', '文献综述', '数据清洗', '其它项目']
    states = [state, 'working', 'working', 'done', 'unknown', 'idle', 'idle']
    rows = []
    for i in range(count):
        candidate = states[min(i, len(states) - 1)]
        status = candidate if PRIORITY[candidate] >= PRIORITY[state] else state
        rows.append({'agent': 'codex' if i < 3 else 'claude', 'sid': f'demo-{i + 1:02d}',
                     'project': projects[i % len(projects)], 'state': status,
                     'elapsed': 132 + i * 37, 'age': i + 1})
    return {'state': state if rows else 'idle', 'sessions': rows, 'updated': now}
