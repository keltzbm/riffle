"""Keep analysis and scheduler backends independent of their callers."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_metagame_analysis_imports_without_ingestion_or_database_code():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import riffle.analysis.metagame; "
            "assert not any(name.startswith(('riffle.ingest', 'riffle.db', 'typer', 'sqlalchemy')) "
            "for name in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_scheduler_backends_depend_on_shared_jobs_without_dispatch_or_each_other():
    for backend in ("launchd", "systemd"):
        source = ROOT / "src" / "riffle" / "ops" / "schedule" / f"{backend}.py"
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "riffle.ops.schedule":
                assert {alias.name for alias in node.names} <= {"jobs"}
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in {
                    "riffle.ops.schedule.dispatch",
                    "riffle.ops.schedule.launchd",
                    "riffle.ops.schedule.systemd",
                }
