# SPDX-License-Identifier: Apache-2.0
"""The Apache-2.0 boundary: gap_core and gap_verify import nothing from the noncommercial packages.

If this fails, a commercial user of the open verifier would need a PolyForm Noncommercial package,
which breaks the point of the split (LICENSING.md).
"""
import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]
APACHE = {"gap_core", "gap_verify"}


def _gap_imports(root: Path):
    for f in root.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        for node in ast.walk(ast.parse(f.read_text(), filename=str(f))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                if top.startswith("gap_"):
                    yield f, top


def test_apache_packages_import_only_apache_packages():
    bad = [(str(f.relative_to(PACKAGES)), top) for pkg in ("core", "verify")
           for f, top in _gap_imports(PACKAGES / pkg / "src") if top not in APACHE]
    assert not bad, bad


def test_apache_packages_declare_apache_and_ship_the_text():
    for pkg in ("core", "verify"):
        assert 'license = "Apache-2.0"' in (PACKAGES / pkg / "pyproject.toml").read_text()
        assert "Apache License" in (PACKAGES / pkg / "LICENSE").read_text()
        for f in (PACKAGES / pkg / "src").rglob("*.py"):
            if "__pycache__" not in f.parts:
                head = "\n".join(f.read_text().splitlines()[:2])
                assert "SPDX-License-Identifier: Apache-2.0" in head, f
