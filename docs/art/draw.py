"""Draw Riffle's logo, favicon, banner and social card into docs/.

Every file comes from one drawing of the mark: nested arches, each made of a left half (one packet
of cards) and a right half (the other packet). At the top of each arch one half runs past the middle
and the other stops short of it, and the arches take turns, which is how two packets interleave in a
riffle shuffle. The lettering is drawn as outlines from Inter, so it looks the same on every computer
whatever fonts it has.

The colors are the brand's: purple #A855F7 and orange #F97316 for the two packets, deep purple
#3B0764 for the lettering. They were chosen to stay distinct for people with red-green or blue-yellow
color blindness (simulated with Machado, Oliveira and Fernandes, 2009); the shapes show the
interleaving without them.

Run it from the repository root with the folder of Inter's static fonts (https://github.com/rsms/inter,
SIL Open Font License). It writes the SVGs, then the three files that have to be pictures rather than
drawings, with resvg and Pillow: the social card (og.png), the favicon for older browsers (favicon.ico)
and the icon a phone shows on its home screen (apple-touch-icon.png).

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

PURPLE, ORANGE, INK, MUTED = "#A855F7", "#F97316", "#3B0764", "#6B6670"
PAPER, EDGE = "#FBFAF8", "#E7E3EA"
DOCS = Path(__file__).resolve().parent.parent
# Light lettering when the page around the image is dark.
DARK = (
    "<style>.ink{fill:#3B0764}.muted{fill:#6B6670}"
    "@media (prefers-color-scheme:dark){.ink{fill:#EDE9F5}.muted{fill:#A39DAB}}</style>"
)


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


def tile_arches() -> str:
    """The mark as drawn on the favicon's square: a little smaller, so it sits clear of the edges."""
    return arches(60, 86, [42, 30, 18], 8, 8, 4)


def deck(x: float, y: float, width: float, cards: int) -> str:
    """The cards after the shuffle: thin card edges, purple and orange in turn, squared up a little
    unevenly, as a deck is."""
    return "".join(
        f'<rect x="{x + (i * 7) % 12}" y="{y + i * 6}" width="{width}" height="4" rx="2" '
        f'fill="{PURPLE if i % 2 == 0 else ORANGE}"/>'
        for i in range(cards)
    )


def illustration(cx: float = 955, cy: float = 300) -> str:
    """Two packets bending into the riffle's arch, and the deck they fall into below it."""
    radii = [190 - 13 * i for i in range(10)]
    return arches(cx, cy, radii, 7, 5, 7) + deck(cx - 143, cy + 16, 276, 10)


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
    """The mark and the name side by side, as at the banner's top left, and the name's right edge."""
    name, width = bold.path("Riffle", 92, 196, 178, tracking=-1)
    drawn = f'<g transform="translate(70 90) scale(.95)">{mark()}</g><path class="ink" d="{name}"/>'
    return drawn, 196 + width


def banner(bold: Lettering, regular: Lettering) -> str:
    """The banner for the README: the name and what Riffle is on the left, the shuffle on the right."""
    tagline, _ = regular.path("Card game analytics, with the math shown.", 30, 74, 252)
    topics, _ = regular.path("Metagame share · Deck-building odds · Price history", 22, 74, 300)
    # The banner always sits on its own light card, so its lettering keeps the light-page colors.
    name = lockup(bold)[0].replace('class="ink"', f'fill="{INK}"')
    body = (
        f'<rect x="1" y="1" width="1278" height="418" rx="28" fill="{PAPER}" '
        f'stroke="{EDGE}" stroke-width="2"/>'
        f"{illustration()}"
        f'<line x1="740" y1="384" x2="1170" y2="384" stroke="{EDGE}" stroke-width="2"/>'
        f'{name}<path fill="{INK}" d="{tagline}"/><path fill="{MUTED}" d="{topics}"/>'
    )
    return svg(
        1280,
        420,
        body,
        "Riffle: card game analytics, with the math shown",
        "The Riffle logo and name, and the line Card game analytics, with the math shown: metagame share, "
        "deck-building odds and price history. Beside them, two packets of cards, purple and orange, bend "
        "into an arch and interleave, the way a riffle shuffle does, and fall into one deck below.",
    )


def social(bold: Lettering, regular: Lettering) -> str:
    """The card a link to riffletcg.gg shows when it's shared: the banner on a 1200 by 630 page."""
    inner = banner(bold, regular)
    inner = inner[inner.index("</desc>") + len("</desc>") : inner.rindex("</svg>")]
    # The banner's own rounded card is left out: the social card's background fills its whole page.
    card = inner[: inner.index("/>") + 2]
    body = (
        f'<rect width="1200" height="630" fill="{PAPER}"/>'
        f'<g transform="translate(-4 85) scale(.94)">{inner.replace(card, "")}</g>'
    )
    title = "Riffle: card game analytics, with the math shown"
    return svg(1200, 630, body, title, "The Riffle banner, for link previews.")


def main(fonts: Path) -> None:
    bold, regular = Lettering(fonts / "Inter-Bold.ttf"), Lettering(fonts / "Inter-Regular.ttf")
    files = {
        "logo.svg": svg(
            120, 120, mark(), "Riffle", "The Riffle logo: purple and orange arches interleaving at the top."
        ),
        "favicon.svg": svg(
            120,
            120,
            f'<rect x="4" y="4" width="112" height="112" rx="26" fill="{INK}"/>' + tile_arches(),
            "Riffle",
            "The Riffle logo on a deep purple square.",
        ),
        "art/touch-icon.svg": svg(
            180,
            180,
            f'<rect width="180" height="180" fill="{INK}"/><g transform="translate(15 15) scale(1.25)">'
            + tile_arches()
            + "</g>",
            "Riffle",
            "The Riffle logo on a deep purple square, for a phone's home screen.",
        ),
        "banner.svg": banner(bold, regular),
        "art/social.svg": social(bold, regular),
        "shuffle.svg": svg(
            440,
            300,
            f'<g transform="translate(-735 -95)">{illustration()}</g>',
            "A riffle shuffle",
            "Two packets of cards, purple and orange, bend into an arch and interleave, the way a riffle "
            "shuffle does, and fall into one deck below.",
        ),
    }
    name, right = lockup(bold)
    files["wordmark.svg"] = svg(
        right - 70 + 8,
        96,
        DARK + name,
        "Riffle",
        "The Riffle logo and name.",
        box=f"70 100 {number(right - 70 + 8)} 96",
    )
    for path, text in files.items():
        (DOCS / path).write_text(text, encoding="utf-8")
        print(f"wrote docs/{path} ({len(text.encode()):,} bytes)", flush=True)
    picture("art/social.svg", 1200, 630).save(DOCS / "og.png")
    picture("art/touch-icon.svg", 180, 180).save(DOCS / "apple-touch-icon.png")
    # One file holding the favicon at 16, 32 and 48 pixels, for browsers that don't read favicon.svg.
    picture("favicon.svg", 256, 256).save(DOCS / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    for out in ("og.png", "apple-touch-icon.png", "favicon.ico"):
        print(f"wrote docs/{out} ({(DOCS / out).stat().st_size:,} bytes)", flush=True)


def picture(source: str, width: int, height: int) -> Image.Image:
    """An SVG in docs/ drawn as a picture of the given size, its transparent parts kept."""
    png = resvg_py.svg_to_bytes(svg_path=str(DOCS / source), width=width, height=height)
    return Image.open(io.BytesIO(bytes(png)))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser())
