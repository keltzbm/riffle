"""The site at riffletcg.gg (docs/) and the README link only files that are in the repository."""

import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def test_every_file_the_page_links_is_in_docs():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    linked = set(re.findall(r'(?:href|src)="([^":#]+)"', page))
    assert {"favicon.svg", "favicon.ico", "apple-touch-icon.png", "wordmark.svg"} <= linked
    assert sorted(name for name in linked if not (DOCS / name).is_file()) == []


def test_the_link_preview_and_the_readme_logo_are_in_the_repository():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    assert '<meta property="og:image" content="https://riffletcg.gg/link-preview.png">' in page
    assert (DOCS / "link-preview.png").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '<img src="docs/wordmark.svg"' in readme and (DOCS / "wordmark.svg").is_file()


def test_the_page_and_the_readme_open_with_what_riffle_is():
    opening = "Riffle is a free, open-source tool for trading card game data and math."
    page = " ".join((DOCS / "index.html").read_text(encoding="utf-8").split())
    assert f'<meta name="description" content="{opening}">' in page
    assert f'<meta property="og:description" content="{opening}">' in page
    assert f'<p class="first">{opening}</p>' in page
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert f"{opening} Riffle is in development. So far it:" in readme


def test_the_page_gives_the_fan_content_notice_word_for_word():
    # Wizards of the Coast's Fan Content Policy requires this notice exactly as written.
    notice = (
        "Riffle is unofficial Fan Content permitted under the Fan Content Policy. "
        "Not approved/endorsed by Wizards. "
        "Portions of the materials used are property of Wizards of the Coast. ©Wizards of the Coast LLC."
    )
    page = " ".join((DOCS / "index.html").read_text(encoding="utf-8").split())
    assert notice in page


def test_the_not_found_page_links_from_the_site_root():
    # GitHub Pages serves 404.html at any missing address, however deep, so its links start at the root.
    page = (DOCS / "404.html").read_text(encoding="utf-8")
    linked = set(re.findall(r'(?:href|src)="([^"#]+)"', page))
    assert linked and all(name.startswith("/") for name in linked)
    assert sorted(name for name in linked if not (DOCS / (name.lstrip("/") or "index.html")).is_file()) == []


def png_size(path: Path) -> tuple[int, int]:
    """Return the width and height a PNG file's header gives."""
    width, height = struct.unpack(">II", path.read_bytes()[16:24])
    return width, height


def test_the_link_previews_are_the_sizes_they_say():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    width = int(re.findall(r'<meta property="og:image:width" content="(\d+)">', page)[0])
    height = int(re.findall(r'<meta property="og:image:height" content="(\d+)">', page)[0])
    assert png_size(DOCS / "link-preview.png") == (width, height)
    # GitHub fits a repository's preview to 2:1 and takes a file of at most 1 MB.
    github = DOCS / "art" / "github-preview.png"
    across, down = png_size(github)
    assert across == 2 * down and github.stat().st_size < 1_000_000


def test_the_tab_icon_stands_alone_and_the_phone_icon_is_opaque():
    # On a tab the logo stands alone; a phone fills a transparent icon with black, so its icon has none.
    assert "<rect" not in (DOCS / "favicon.svg").read_text(encoding="utf-8")
    # A PNG's color type, byte 25 of the file, is 2 for color without transparency.
    assert (DOCS / "apple-touch-icon.png").read_bytes()[25] == 2
