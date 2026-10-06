"""The README, the design note, and the contributing guide work for anyone who clones Riffle."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDES = ["README.md", "DESIGN.md", "CONTRIBUTING.md"]


def test_the_guides_name_no_one_s_own_folders():
    for name in GUIDES:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "~/atelier" not in text and "/Users/" not in text, name


def test_the_guides_link_only_files_in_the_repository():
    for name in GUIDES:
        text = (ROOT / name).read_text(encoding="utf-8")
        targets = re.findall(r"\]\((?!https?:|#|mailto:)([^)#\s]+)", text)
        assert [target for target in targets if not (ROOT / target).exists()] == [], name


def test_issues_open_with_a_form_and_security_problems_go_privately():
    forms = ROOT / ".github" / "ISSUE_TEMPLATE"
    names = sorted(form.name for form in forms.glob("*.yml"))
    assert names == ["1-bug.yml", "2-wrong-data.yml", "3-idea.yml", "config.yml"]
    config = (forms / "config.yml").read_text(encoding="utf-8")
    assert "blank_issues_enabled: false" in config
    assert "https://github.com/keltzbm/riffle/security/advisories/new" in config
