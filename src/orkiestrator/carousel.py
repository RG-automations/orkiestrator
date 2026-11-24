"""Carousel slides as 1080x1350 SVG files (stdlib only; open in a browser or import into Canva/Figma)."""
import textwrap
from pathlib import Path
from xml.sax.saxutils import escape

from .db import ROOT

W, H = 1080, 1350


def slide_svg(title, text, index, total, brand="#0a66c2"):
    lines = textwrap.wrap(text, 30)[:12]
    head = textwrap.wrap(title, 22)[:3]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
             f'<rect width="{W}" height="{H}" fill="#ffffff"/><rect width="{W}" height="24" fill="{brand}"/>']
    y = 260
    for line in head:
        parts.append(f'<text x="80" y="{y}" font-family="Arial, sans-serif" font-size="78" font-weight="700" fill="#111">{escape(line)}</text>')
        y += 96
    y += 50
    for line in lines:
        parts.append(f'<text x="80" y="{y}" font-family="Arial, sans-serif" font-size="46" fill="#333">{escape(line)}</text>')
        y += 64
    parts.append(f'<text x="80" y="{H - 70}" font-family="Arial, sans-serif" font-size="34" fill="#888">{index}/{total}</text></svg>')
    return "\n".join(parts)


def assets_dir(post_id, root=None):
    return Path(root or ROOT / "data" / "assets") / str(post_id)


def render(post_id, slides, root=None):
    """slides: list of {title, text}. Returns the written file paths."""
    out = assets_dir(post_id, root)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, s in enumerate(slides, 1):
        p = out / f"slide-{i:02d}.svg"
        p.write_text(slide_svg(str(s.get("title", "")), str(s.get("text", "")), i, len(slides)), encoding="utf-8")
        paths.append(p)
    return paths
