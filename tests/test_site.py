"""The site at riffletcg.gg (docs/) and the README link only files that are in the repository."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def test_every_file_the_page_links_is_in_docs():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    linked = set(re.findall(r'(?:href|src)="([^":#]+)"', page))
    assert {"favicon.svg", "favicon.ico", "apple-touch-icon.png", "wordmark.svg", "shuffle.svg"} <= linked
    assert sorted(name for name in linked if not (DOCS / name).is_file()) == []


def test_the_social_card_and_the_readme_banner_are_in_the_repository():
    page = (DOCS / "index.html").read_text(encoding="utf-8")
    assert '<meta property="og:image" content="https://riffletcg.gg/og.png">' in page
    assert (DOCS / "og.png").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '<img src="docs/banner.svg"' in readme and (DOCS / "banner.svg").is_file()


def test_the_page_and_the_readme_open_with_what_riffle_is():
    opening = "Riffle is a free, open-source tool for the math of trading card games, and shows its work."
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
