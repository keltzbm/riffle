"""Draw Riffle's logo, favicon, wordmark and link previews into docs/.

Every file comes from one drawing of the mark: nested arches, each made of a left half (one packet
of cards) and a right half (the other packet). At the top of each arch one half runs past the middle
and the other stops short of it, and the arches take turns, which is how two packets interleave in a
riffle shuffle. The mark is three arches everywhere: beside the name, as tall as its R, and alone in
the browser tab and on a phone's home screen. The lettering is drawn as outlines from Inter, so it
looks the same on every computer whatever fonts it has.

The colors are the brand's: purple #A855F7 and orange #F97316 for the two packets, deep purple
#3B0764 for the lettering. They were chosen to stay distinct for people with red-green or blue-yellow
color blindness (simulated with Machado, Oliveira and Fernandes, 2009); the shapes show the
interleaving without them.

Run it from the repository root with the folder of Inter's static fonts (https://github.com/rsms/inter,
SIL Open Font License). It writes the SVGs, then the files that have to be pictures rather than
drawings, with resvg and Pillow: the preview a shared link shows (link-preview.png) and the one GitHub
shows for the repository (art/github-preview.png), each at twice the size asked for so they stay sharp
on high-resolution screens; the favicon for older browsers (favicon.ico); and the icon a phone shows on
its home screen (apple-touch-icon.png).

    uv run --with fonttools --with uharfbuzz --with resvg-py --with pillow \
        python docs/art/draw.py ~/Downloads/inter/extras/ttf
"""

import io
import math
import sys
from pathlib import Path

import resvg_py
import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

PURPLE, ORANGE, INK, PAPER = "#A855F7", "#F97316", "#3B0764", "#FBFAF8"
DOCS = Path(__file__).resolve().parent.parent
# Light lettering when the page around the image is dark.
DARK = "<style>.ink{fill:#3B0764}@media (prefers-color-scheme:dark){.ink{fill:#EDE9F5}}</style>"


def number(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def arc(cx: float, cy: float, r: float, start: float, end: float, color: str, width: float) -> str:
    """An arc of radius r drawn clockwise from angle `start` to `end`, in degrees: 0 is to the
    right of the centre, 90 straight above it, 180 to the left."""
    x0, y0 = cx + r * math.cos(math.radians(start)), cy - r * math.sin(math.radians(start))
    x1, y1 = cx + r * math.cos(math.radians(end)), cy - r * math.sin(math.radians(end))
    return (
        f'<path d="M{number(x0)} {number(y0)}A{r} {r} 0 0 1 {number(x1)} {number(y1)}" fill="none" '
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>'
    )


def arches(cx: float, cy: float, radii: list[float], width: float, cross: float, gap: float) -> str:
    """The mark's nested arches around (cx, cy), outermost first.

    On each arch one half runs `cross` degrees past the top, and the other half stops so that a
    gap of `gap` pixels is left between the two rounded ends. The gap is a length, not an angle,
    so it looks the same on a small arch as on a large one. Even-numbered arches let the left half
    run past the top, odd-numbered ones the right half.
    """
    out = []
    for i, r in enumerate(radii):
        stop = cross + math.degrees((gap + width) / r)
        left_ends, right_starts = (90 - cross, 90 - stop) if i % 2 == 0 else (90 + stop, 90 + cross)
        out += [arc(cx, cy, r, 180, left_ends, PURPLE, width), arc(cx, cy, r, right_starts, 0, ORANGE, width)]
    return "".join(out)


def mark() -> str:
    """The mark in a 120 by 120 square."""
    return arches(60, 92, [48, 36, 24], 8, 8, 4)


def tab_icon() -> str:
    """The mark as large as the favicon's square allows, so it still reads at 16 pixels.

    Its ink, from x 8 to 112 and y 40 to 96 in its own square, is scaled 1.1 about the middle of the
    ink, which lands in the middle of the square. There is no background: on a tab it is the logo itself.
    """
    return f'<g transform="translate(60 60) scale(1.1) translate(-60 -68)">{mark()}</g>'


def tile_arches() -> str:
    """The mark drawn a little smaller, clear of the corners a phone rounds off its icons."""
    return arches(60, 86, [42, 30, 18], 8, 8, 4)


class Lettering:
    """Text drawn as outlines from one of Inter's static fonts, shaped by HarfBuzz for kerning."""

    def __init__(self, path: Path) -> None:
        self.font = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
        self.tt = TTFont(path)
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.em = self.tt["head"].unitsPerEm

    def path(self, text: str, size: float, x: float, y: float, tracking: float = 0) -> tuple[str, float]:
        """The outline of `text` with its baseline starting at (x, y), and its width. `tracking`
        is extra space between letters, in pixels."""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.font, buf, {"kern": True, "liga": True})
        scale = size / self.em
        pen = SVGPathPen(self.glyphs, ntos=number)
        advance = 0.0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions, strict=True):
            at = (scale, 0, 0, -scale, x + (advance + pos.x_offset) * scale, y - pos.y_offset * scale)
            self.glyphs[self.order[info.codepoint]].draw(TransformPen(pen, at))
            advance += pos.x_advance + tracking / scale
        return pen.getCommands(), advance * scale


def svg(width: float, height: float, body: str, title: str, desc: str, box: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{number(width)}" height="{number(height)}" '
        f'viewBox="{box or f"0 0 {number(width)} {number(height)}"}" role="img">'
        f"<title>{title}</title><desc>{desc}</desc>{body}</svg>\n"
    )


def lockup(bold: Lettering) -> tuple[str, float]:
    """The mark and the name side by side, and the name's right edge.

    The name is set at 92 pixels on a baseline at y 178, so its R runs from y 111.1 to the baseline.
    The mark's ink, from y 40 to 96 and x 8 to 112 in its own square, is scaled to run the same
    height, starts at x 77.6, and stands 30 pixels left of the R's ink, which starts 6.1 pixels right
    of where the name is placed.
    """
    scale = 66.9 / 56
    mark_right = 77.6 + 104 * scale
    x = mark_right + 30 - 6.1
    name, width = bold.path("Riffle", 92, x, 178, tracking=-1)
    place = f"translate({number(77.6 - 8 * scale)} {number(178 - 96 * scale)}) scale({scale:.4f})"
    return f'<g transform="{place}">{mark()}</g><path class="ink" d="{name}"/>', x + width


def preview(bold: Lettering, width: int, height: int, what: str) -> str:
    """A link preview: the logo and name, centred, on a page of the given size.

    Some apps crop a preview to a square from its middle, so on a page 630 high the logo and name are
    540 wide, inside that square, and they take the same share of the height on a page of another size.
    """
    # The preview always has its own light background, so its lettering keeps the light-page colors.
    name, right = lockup(bold)
    name = name.replace('class="ink"', f'fill="{INK}"')
    # The lockup's ink runs from x 77.6 to the name's right edge, and from the i's dot at y 107.4 to
    # the e's curve just under the baseline, at 179.3.
    scale = 540 / (right - 77.6) * height / 630
    middle_x, middle_y = (77.6 + right) / 2, (107.4 + 179.3) / 2
    left, top = width / 2 - middle_x * scale, height / 2 - middle_y * scale
    place = f"translate({number(left)} {number(top)}) scale({scale:.4f})"
    body = f'<rect width="{width}" height="{height}" fill="{PAPER}"/><g transform="{place}">{name}</g>'
    return svg(width, height, body, "Riffle", f"The Riffle logo and name, {what}.")


def main(fonts: Path) -> None:
    bold = Lettering(fonts / "Inter-Bold.ttf")
    files = {
        "logo.svg": svg(
            120, 120, mark(), "Riffle", "The Riffle logo: purple and orange arches interleaving at the top."
        ),
        "favicon.svg": svg(120, 120, tab_icon(), "Riffle", "The Riffle logo, for the browser tab."),
        "art/touch-icon.svg": svg(
            180,
            180,
            # White, not transparent: a phone fills a transparent icon with black.
            '<rect width="180" height="180" fill="#FFFFFF"/><g transform="translate(15 15) scale(1.25)">'
            + tile_arches()
            + "</g>",
            "Riffle",
            "The Riffle logo on white, for a phone's home screen.",
        ),
        "art/link-preview.svg": preview(bold, 1200, 630, "for link previews"),
        "art/github-preview.svg": preview(bold, 1280, 640, "for GitHub's preview of the repository"),
    }
    # The wordmark's box leaves about 8 pixels around the ink on every side, so it centres evenly.
    name, right = lockup(bold)
    files["wordmark.svg"] = svg(
        right - 70 + 8,
        87,
        DARK + name,
        "Riffle",
        "The Riffle logo and name.",
        box=f"70 100 {number(right - 70 + 8)} 87",
    )
    for path, text in files.items():
        (DOCS / path).write_text(text, encoding="utf-8")
        print(f"wrote docs/{path} ({len(text.encode()):,} bytes)", flush=True)
    # Each preview at twice the size the apps and GitHub ask for, so it stays sharp on high-resolution
    # screens. GitHub takes a file of at most 1 MB; these flat colors come to far less.
    picture("art/link-preview.svg", 2400, 1260).convert("RGB").save(DOCS / "link-preview.png", optimize=True)
    picture("art/github-preview.svg", 2560, 1280).convert("RGB").save(
        DOCS / "art/github-preview.png", optimize=True
    )
    picture("art/touch-icon.svg", 180, 180).convert("RGB").save(DOCS / "apple-touch-icon.png", optimize=True)
    # One file holding the favicon at 16, 32 and 48 pixels, for browsers that don't read favicon.svg.
    picture("favicon.svg", 256, 256).save(DOCS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    for out in ("link-preview.png", "art/github-preview.png", "apple-touch-icon.png", "favicon.ico"):
        print(f"wrote docs/{out} ({(DOCS / out).stat().st_size:,} bytes)", flush=True)


def picture(source: str, width: int, height: int) -> Image.Image:
    """An SVG in docs/ drawn as a picture of the given size, its transparent parts kept."""
    png = resvg_py.svg_to_bytes(svg_path=str(DOCS / source), width=width, height=height)
    return Image.open(io.BytesIO(bytes(png)))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
