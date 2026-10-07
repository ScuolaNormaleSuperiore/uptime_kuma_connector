"""Guards the plugin layout against what Cheshire Cat imports.

The Cat imports every `.py` that `glob("**/*.py", recursive=True)` returns for
the plugin folder, with no exclusions. Tests therefore live in `.tests/`, which
that glob does not enter, and this file fails when a `.py` appears that the Cat
would import although no plugin code needs it.

Unit tier: plain `pytest`, no `cat` import.
"""

import ast
import glob
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# The module that defines the tool, the hooks and the settings model: where the
# import graph starts.
ENTRY_POINT = "uptime_kuma_connector"

# Scripts that run nothing on import (guarded by `__main__`) and are never
# imported by plugin code. Anything else the Cat would import must be reachable
# from the entry point.
LISTED_EXCEPTIONS = {"run-tests.py", "package-plugin.py"}


def _cat_imports() -> set[str]:
    """What the Cat's own glob returns, relative to the plugin root."""
    found = glob.glob("**/*.py", root_dir=PLUGIN_ROOT, recursive=True)
    return {Path(name).as_posix() for name in found if not name.startswith("DEV/")}


def _local_imports(module_file: Path) -> set[str]:
    """Top-level plugin modules imported by `module_file`, absolute or relative."""
    tree = ast.parse(module_file.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            else:
                names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
    return {name for name in names if (PLUGIN_ROOT / f"{name}.py").is_file()}


def _reachable_from_entry_point() -> set[str]:
    seen = {ENTRY_POINT}
    pending = [ENTRY_POINT]
    while pending:
        current = pending.pop()
        for name in _local_imports(PLUGIN_ROOT / f"{current}.py") - seen:
            seen.add(name)
            pending.append(name)
    return {f"{name}.py" for name in seen}


class TestCatImportSurface:
    def test_tests_are_not_imported_by_the_cat(self):
        imported = _cat_imports()

        in_tests = sorted(name for name in imported if name.startswith(".tests/"))
        assert not in_tests, f"the Cat would import test files: {in_tests}"
        assert (PLUGIN_ROOT / ".tests").is_dir(), "the .tests/ folder is missing"

    def test_every_imported_file_is_needed_by_the_plugin(self):
        unneeded = _cat_imports() - _reachable_from_entry_point() - LISTED_EXCEPTIONS

        assert not unneeded, (
            f"the Cat would import {sorted(unneeded)}, but no plugin code needs "
            "them. Move them under .tests/ or list them in LISTED_EXCEPTIONS."
        )

    def test_listed_exceptions_still_exist(self):
        for name in LISTED_EXCEPTIONS:
            assert (PLUGIN_ROOT / name).is_file(), f"'{name}' is listed but missing"
