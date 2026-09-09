"""Guards the explicit file list in `package-plugin.py`.

Unit tier: no `cat` import, plain `pytest`. `package-plugin.py` is a script,
not an importable module — its name has a hyphen — so it is loaded here with
`importlib` from its path rather than with a normal `import` statement.

The Cat imports every `.py` it finds in the plugin folder, this file included,
as `cat.plugins.uptime_kuma_connector.tests.unit.test_packaging`. Loading and
executing `package-plugin.py`'s module code has no reason to happen inside the
core process, so it is guarded the same way the other test modules guard their
path fix: it runs under `pytest` and nowhere else.
"""

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

if not __name__.startswith("cat.plugins."):
    sys.path.insert(0, str(PLUGIN_ROOT))

    _spec = importlib.util.spec_from_file_location(
        "package_plugin", PLUGIN_ROOT / "package-plugin.py"
    )
    package_plugin = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(package_plugin)  # noqa: E402  (path fix runs first, on purpose)


def _top_level_python_modules() -> set[str]:
    """Every `.py` file directly under the plugin root — not `tests/`, not `DEV/`."""
    return {path.name for path in PLUGIN_ROOT.glob("*.py")}


class TestRuntimeFileList:
    def test_every_top_level_module_is_accounted_for(self):
        # The invariant this file exists for: a new top-level .py file must be
        # a deliberate choice — shipped, or explicitly excluded — not an
        # accident a build script misses because it only checks the files it
        # already knows about.
        accounted_for = set(package_plugin.RUNTIME_FILES) | set(
            package_plugin.DEVELOPMENT_ONLY_MODULES
        )
        unaccounted = _top_level_python_modules() - accounted_for

        assert not unaccounted, (
            f"{sorted(unaccounted)} exist in the plugin folder but are listed "
            "in neither RUNTIME_FILES nor DEVELOPMENT_ONLY_MODULES in "
            "package-plugin.py. Decide whether each ships before adding it "
            "to one of the two lists."
        )

    def test_runtime_files_all_exist(self):
        for name in package_plugin.RUNTIME_FILES:
            assert (PLUGIN_ROOT / name).is_file(), f"'{name}' is listed but missing"

    def test_no_file_is_listed_twice(self):
        assert len(package_plugin.RUNTIME_FILES) == len(set(package_plugin.RUNTIME_FILES))

    def test_runtime_and_development_only_lists_do_not_overlap(self):
        assert not (
            set(package_plugin.RUNTIME_FILES) & set(package_plugin.DEVELOPMENT_ONLY_MODULES)
        )

    def test_settings_json_is_never_shipped(self):
        # The one file that must never leave this machine: it may hold a real
        # Uptime Kuma API key in clear text.
        assert "settings.json" not in package_plugin.RUNTIME_FILES

    def test_development_and_private_content_is_never_shipped(self):
        forbidden_markers = ("test", "dev", "agents")
        for name in package_plugin.RUNTIME_FILES:
            lowered = name.lower()
            assert not any(marker in lowered for marker in forbidden_markers), (
                f"'{name}' looks like development content and should not ship"
            )


class TestBuild:
    def test_build_produces_a_zip_containing_every_runtime_file(self, tmp_path):
        archive_path = package_plugin.build(output_dir=tmp_path)

        assert archive_path.exists()

        import zipfile

        with zipfile.ZipFile(archive_path) as archive:
            shipped = set(archive.namelist())

        expected = {
            f"{package_plugin.PLUGIN_NAME}/{name}"
            for name in package_plugin.RUNTIME_FILES
        }
        assert shipped == expected

    def test_build_fails_loudly_if_a_listed_file_is_missing(self, tmp_path, monkeypatch):
        # A package silently short one file fails at import on a clean
        # installation, far from whoever built it. The build must refuse
        # instead of shipping a partial plugin.
        monkeypatch.setattr(
            package_plugin,
            "RUNTIME_FILES",
            package_plugin.RUNTIME_FILES + ("this-file-does-not-exist.py",),
        )

        try:
            package_plugin.build(output_dir=tmp_path)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("build() should have raised FileNotFoundError")
