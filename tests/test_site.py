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
