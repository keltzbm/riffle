"""Nothing under src/ names the author's machine or account, and the README's shell blocks run in
any POSIX shell, on a Mac or on Linux."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
README = ROOT / "README.md"
NAMES = ("atelier", "keltz")  # the author's folders and account, in any case
HOMES = ("/Users/", "/home/")  # one machine's home folders
PROJECT_URL = "github.com/keltzbm/riffle"  # the repository, whose name can't change
FENCE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.M | re.S)
SHELLS = {"bash", "sh", "shell", "zsh", "console"}
ZSH_ONLY = re.compile(r"\b(?:print -r|setopt)\b")
# a crontab line, five fields or an @daily, with riffle sync as its command
CRON_LINE = re.compile(r"^\s*(?:@\w+|(?:[\d*,/-]+\s+){5}).*\briffle sync\b", re.M)
TIMERS = ("systemd", "systemctl", "OnCalendar")


def own_names(root: Path) -> list[str]:
    """Each line under root that names the author's folders, account or machine, as
    path:line: text; the project's URL on GitHub doesn't count."""
    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            bare = line.replace(PROJECT_URL, "")
            if any(name in bare.lower() for name in NAMES) or any(home in bare for home in HOMES):
                found.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
    return found


def shell_blocks(text: str) -> list[str]:
    """The bodies of the shell code blocks in a Markdown text."""
    return [body for lang, body in FENCE.findall(text) if lang in SHELLS]


def zsh_builtins(text: str) -> list[str]:
    """Each line of a shell code block that only zsh runs: bash says `print: command not found`,
    and `sh -n` passes both, so a parse check can't catch them."""
    return [line for body in shell_blocks(text) for line in body.splitlines() if ZSH_ONLY.search(line)]


def test_the_source_names_no_one_s_machine():
    assert own_names(SRC) == []


def test_the_check_finds_a_planted_name_and_passes_the_project_url(tmp_path):
    (tmp_path / "a.py").write_text('VAULT = "~/atelier/library"\n', encoding="utf-8")
    (tmp_path / "b.py").write_text(f'URL = "https://{PROJECT_URL}"\n', encoding="utf-8")
    (tmp_path / "c.py").write_text('LABEL = "com.keltzbm.riffle"\nHOME = "/home/pat"\n', encoding="utf-8")
    assert own_names(tmp_path) == [
        'a.py:1: VAULT = "~/atelier/library"',
        'c.py:1: LABEL = "com.keltzbm.riffle"',
        'c.py:2: HOME = "/home/pat"',
    ]


def test_the_readme_s_shell_blocks_run_in_bash_too():
    assert zsh_builtins(README.read_text(encoding="utf-8")) == []


def test_the_check_finds_a_planted_zsh_builtin():
    planted = (
        "Set the password:\n\n```zsh\npw=$(openssl rand -hex 24)\n"
        'print -r -- "POSTGRES_PASSWORD=$pw" > .env\n```\n\n'
        "`print -r` in prose is fine.\n\n```bash\nsetopt extended_glob\n```\n\n```python\nsetopt = 1\n```\n"
    )
    assert zsh_builtins(planted) == ['print -r -- "POSTGRES_PASSWORD=$pw" > .env', "setopt extended_glob"]


def test_the_readme_schedules_on_macos_only_and_gives_cron_for_linux():
    text = README.read_text(encoding="utf-8")
    assert re.search(r"macOS[ -]only|only on macOS", text), "the schedule section doesn't say macOS only"
    assert CRON_LINE.search(text), "no cron line running riffle sync"
    assert not any(word in body for _, body in FENCE.findall(text) for word in TIMERS)


CONTRIBUTING = ROOT / "CONTRIBUTING.md"
INSTALL_ORDER = ["uv sync", "chmod 600 ~/.pgpass", "riffle db up", "riffle init --vault", "riffle sync"]


def test_the_readme_s_install_path_runs_top_to_bottom_on_a_fresh_machine():
    blocks = "\n".join(shell_blocks(README.read_text(encoding="utf-8")))
    places = [blocks.find(step) for step in INSTALL_ORDER]
    assert -1 not in places, dict(zip(INSTALL_ORDER, places, strict=True))
    assert places == sorted(places), "the install steps aren't in order"
    assert re.search(r"^printf .*POSTGRES_PASSWORD", blocks, re.M), "the password isn't written with printf"


def test_tab_completion_makes_its_folder_and_installs_for_the_user_s_shell():
    blocks = "\n".join(shell_blocks(README.read_text(encoding="utf-8")))
    assert "mkdir -p ~/.local/bin" in blocks and "riffle --install-completion" in blocks


def test_contributing_s_setup_carries_the_env_step():
    blocks = "\n".join(shell_blocks(CONTRIBUTING.read_text(encoding="utf-8")))
    assert ".env" in blocks or "POSTGRES_PASSWORD" in blocks
