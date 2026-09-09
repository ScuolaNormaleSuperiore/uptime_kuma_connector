"""Build a release zip for `uptime_kuma_connector` from an explicit file list.

Cheshire Cat installs a plugin by unpacking this folder's contents into
`cat/plugins/<name>/` on the target instance. What ships has to be exactly the
runtime surface: including `DEV/`, `tests/`, or a `settings.json` that happens
to exist on this machine would ship developer content or, worse, a real
Uptime Kuma API key in clear text.

The list below is explicit rather than inferred by scanning the folder: an
inferred list silently starts shipping any new top-level file the moment it
is created. Its mirror is `tests/unit/test_packaging.py`, which fails when a
top-level Python module exists in the plugin folder but is accounted for in
neither `RUNTIME_FILES` nor `DEVELOPMENT_ONLY_MODULES` here — so a new module
has to be placed in one list or the other on purpose, instead of being missed
by a build that only checks the files it already knows about.

    python package-plugin.py
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

PLUGIN_NAME = "uptime_kuma_connector"
PLUGIN_ROOT = Path(__file__).resolve().parent

# Every file the plugin needs to load and run once installed. Nothing here is
# specific to this machine or to a developer's checkout.
RUNTIME_FILES = (
    "plugin.json",
    "requirements.txt",
    "settings.py",
    "kuma_client.py",
    "uptime_kuma_connector.py",
    "logo.png",
    "README.md",
    "LICENSE",
)

# Top-level Python modules that exist for development only and are
# deliberately never part of a release. Listed here — rather than merely
# absent from RUNTIME_FILES — so the packaging test can tell "we decided not
# to ship this" apart from "nobody has decided yet".
DEVELOPMENT_ONLY_MODULES = (
    "run-tests.py",
    "package-plugin.py",
)


def build(output_dir: Path | None = None) -> Path:
    """Zip `RUNTIME_FILES` into `dist/<plugin name>-<version>.zip`.

    Raises `FileNotFoundError` immediately if a listed file is missing, rather
    than silently producing a package that is one file short — the exact
    failure mode this script exists to prevent, just moved earlier.
    """
    metadata = json.loads((PLUGIN_ROOT / "plugin.json").read_text(encoding="utf-8"))
    version = metadata["version"]

    output_dir = output_dir or (PLUGIN_ROOT / "dist")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{PLUGIN_NAME}-{version}.zip"

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in RUNTIME_FILES:
            source = PLUGIN_ROOT / name
            if not source.is_file():
                raise FileNotFoundError(
                    f"package-plugin.py: '{name}' is listed in RUNTIME_FILES "
                    "but does not exist."
                )
            archive.write(source, arcname=f"{PLUGIN_NAME}/{name}")

    return archive_path


if __name__ == "__main__":
    built = build()
    print(f"Built {built}")
