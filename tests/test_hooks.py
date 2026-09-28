"""The git hooks in .githooks: pre-commit runs ruff, pre-push everything CI's Linux jobs run."""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".githooks"


def ci_commands() -> list[str]:
    """Every one-line uv command CI runs, in order, once each."""
    found = re.findall(r"^\s*- run: (uv .+)$", (ROOT / ".github" / "workflows" / "ci.yml").read_text(), re.M)
    return list(dict.fromkeys(found))


@pytest.mark.parametrize("hook", ["pre-commit", "pre-push"])
def test_each_hook_is_an_executable_shell_script(hook):
    path = HOOKS / hook
    assert path.read_text().startswith("#!/bin/sh\n")
    assert os.access(path, os.X_OK)


def test_pre_commit_runs_ruff_as_ci_does():
    body = (HOOKS / "pre-commit").read_text()
    assert "uv run ruff check src tests" in body and "uv run ruff format --check src tests" in body


def test_pre_push_runs_every_check_ci_runs_with_the_database_tests_required():
    body = (HOOKS / "pre-push").read_text()
    commands = ci_commands()
    assert "uv run pytest --cov-fail-under=90" in commands and "uv run mypy src" in commands
    for command in commands:
        if command != "uv run pytest":  # the macOS jobs', without the floor
            assert command in body, command
    assert "RIFFLE_REQUIRE_POSTGRES=1 uv run pytest --cov-fail-under=90" in body
