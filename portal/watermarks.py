"""Render a viewer-specific watermark image without storing personal images on disk."""
import re
import textwrap
import math
import unicodedata
from html import escape
from urllib.parse import quote


def support_watermark(user):
    def lines(label, value, width):
        value = re.sub(r"[\x00-\x1f\x7f\ud800-\udfff\ufffe\uffff]", " ", str(value))
        value = " ".join(value.split())
        return textwrap.wrap(label + value, width=width, break_long_words=True, break_on_hyphens=False)

    text = lines("姓名：", user.full_name or "未填写", 16)
    text += lines("账号：" if user.is_staff else "学号：", user.username, 22)
    # Denser spacing for ordinary identities; long names still fit after rotation.
    text_width = max(sum(14 if unicodedata.east_asian_width(char) in {"W", "F"} else 9 for char in row) for row in text)
    text_height = len(text) * 20
    angle = math.radians(22)
    width = max(260, math.ceil(text_width * math.cos(angle) + text_height * math.sin(angle) + 32))
    height = max(140, math.ceil(text_width * math.sin(angle) + text_height * math.cos(angle) + 32))
    first_y = (height - (len(text) - 1) * 20) / 2
    rows = "".join(f'<text x="{width / 2}" y="{first_y + index * 20}">{escape(value)}</text>'
                   for index, value in enumerate(text))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
           f'<g transform="rotate(-22 {width / 2} {height / 2})" fill="#4c655b" text-anchor="middle" '
           'font-family="-apple-system,BlinkMacSystemFont,PingFang SC,Microsoft YaHei,sans-serif" font-size="14" font-weight="500">'
           + rows + '</g></svg>')
    return {"url": "data:image/svg+xml;charset=utf-8," + quote(svg, safe=""), "width": width, "height": height}
