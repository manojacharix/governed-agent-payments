"""Architecture test: gap_core does no I/O and never reads the clock (build plan §1)."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "gap_core"

FORBIDDEN_MODULES = {
    "os", "sys", "io", "socket", "ssl", "http", "urllib", "requests", "httpx", "subprocess",
    "sqlite3", "psycopg", "psycopg2", "pathlib", "shutil", "tempfile", "time", "random", "secrets",
    "logging", "threading", "asyncio",
}
FORBIDDEN_CALLS = {"now", "utcnow", "today", "time", "open", "getenv"}
ALLOWED_FILES_FOR_CLOCK = {"clock.py", "ids.py"}  # clock defines the protocol; ids takes clock and rng as arguments


def _modules():
    return sorted(p for p in CORE.rglob("*.py"))


def test_core_imports_nothing_that_does_io():
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            offenders += [f"{path.name}: {n}" for n in names if n in FORBIDDEN_MODULES]
    assert offenders == []


def test_core_never_reads_the_clock_or_files():
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                if name in FORBIDDEN_CALLS and not (name == "now" and path.name in ALLOWED_FILES_FOR_CLOCK):
                    offenders.append(f"{path.name}:{node.lineno} {name}()")
    assert offenders == []
