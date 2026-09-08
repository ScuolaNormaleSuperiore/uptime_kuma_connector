"""Run the uptime_kuma_connector test suite.

Unit tests use the current local Python interpreter. Integration tests need the
Cheshire Cat core importable and therefore run inside its Docker container.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SERVICE = "cheshire-cat-core"
PLUGIN_IN_CONTAINER = "/app/cat/plugins/uptime_kuma_connector"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the uptime_kuma_connector test suite."
    )
    tier = parser.add_mutually_exclusive_group()
    tier.add_argument(
        "-u",
        "--unit",
        action="store_true",
        help="run tests/unit with the current local Python interpreter",
    )
    tier.add_argument(
        "-i",
        "--integration",
        action="store_true",
        help="run only tests/integration inside the Cheshire Cat container",
    )
    parser.add_argument(
        "-d",
        "--detailed",
        action="store_true",
        help="pass -v to pytest and list every test name",
    )
    return parser.parse_args()


def pytest_arguments(detailed: bool, path: str | None = None) -> list[str]:
    arguments = ["python", "-m", "pytest"]
    if path:
        arguments.append(path)
    if detailed:
        arguments.append("-v")
    return arguments


def run_local_unit_tests(detailed: bool) -> int:
    print("Unit tests (pure logic), local interpreter")

    probe = subprocess.run(
        [sys.executable, "-c", "import pytest"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        print("pytest is not installed in this interpreter:", file=sys.stderr)
        print(f"  {sys.executable}", file=sys.stderr)
        print(
            f"Install it with:  {Path(sys.executable).name} -m pip install pytest",
            file=sys.stderr,
        )
        return 1

    arguments = [sys.executable, "-m", "pytest", "tests/unit"]
    if detailed:
        arguments.append("-v")
    return subprocess.run(arguments, cwd=REPO_ROOT, check=False).returncode


def compose_directory() -> Path:
    directory = (REPO_ROOT / "../../../..").resolve()
    compose_file = directory / "compose.yml"
    if not compose_file.is_file():
        raise FileNotFoundError(
            "compose.yml not found where expected:\n"
            f"  {compose_file}\n"
            "This runner expects the plugin under core/cat/plugins/."
        )
    return directory


def detect_compose_command() -> list[str] | None:
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run(
            [docker, "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return [docker, "compose"]

    docker_compose = shutil.which("docker-compose")
    if docker_compose:
        return [docker_compose]
    return None


def running_container_id(compose_command: list[str], directory: Path) -> str:
    result = subprocess.run(
        [*compose_command, "ps", "-q", SERVICE],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def run_container_tests(detailed: bool, integration_only: bool) -> int:
    try:
        project_directory = compose_directory()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1

    compose_command = detect_compose_command()
    if compose_command is None:
        print("Neither 'docker compose' nor 'docker-compose' is available.", file=sys.stderr)
        print("Run unit tests instead:  python run-tests.py --unit", file=sys.stderr)
        return 1

    if not running_container_id(compose_command, project_directory):
        print(f"The '{SERVICE}' container is not running.", file=sys.stderr)
        print(
            f"Start it from {project_directory} with:  "
            f"{' '.join(compose_command)} up -d {SERVICE}",
            file=sys.stderr,
        )
        return 1

    tier = "Integration tests" if integration_only else "Full suite"
    print(f"{tier}, in the '{SERVICE}' container")

    command = [
        *compose_command,
        "exec",
        "-T",
        "-w",
        PLUGIN_IN_CONTAINER,
        SERVICE,
        *pytest_arguments(
            detailed=detailed,
            path="tests/integration" if integration_only else None,
        ),
    ]
    environment = os.environ.copy()
    environment.setdefault("MSYS_NO_PATHCONV", "1")
    return subprocess.run(
        command,
        cwd=project_directory,
        env=environment,
        check=False,
    ).returncode


def main() -> int:
    arguments = parse_args()
    if arguments.unit:
        return run_local_unit_tests(arguments.detailed)
    return run_container_tests(arguments.detailed, arguments.integration)


if __name__ == "__main__":
    raise SystemExit(main())
