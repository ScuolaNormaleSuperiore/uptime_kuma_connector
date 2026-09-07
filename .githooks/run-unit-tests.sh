#!/usr/bin/env bash
set -euo pipefail

echo "[KUMA-CONN pre-commit] Running unit tests..." >&2

# Derived from the script location rather than from `git rev-parse`, so this
# script also runs where git is not installed, for instance inside the core
# container when verifying the hook itself.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

# `tests/unit` needs nothing but pytest and finishes in well under a second,
# because it exercises `kuma_client.py`, which imports nothing from `cat` and
# performs no I/O.
#
# There is no `tests/integration` yet. When it arrives it stays out of this
# hook, for the same reason the sibling plugin keeps it out: it needs the
# Cheshire Cat core importable, in practice the running container, and a commit
# must not depend on Docker being up. Otherwise the hook either blocks
# legitimate commits or skips in silence.
test_target="tests/unit"

if [ ! -d "$test_target" ]; then
	echo "[KUMA-CONN pre-commit] $test_target not found, nothing to run." >&2
	exit 0
fi

python_bin=""
for candidate in python python3 py; do
	if command -v "$candidate" >/dev/null 2>&1; then
		python_bin="$candidate"
		break
	fi
done

# A missing development tool warns but does not block. Blocking here would
# teach `--no-verify`, which would also disable the secret scan: the cure would
# be worse than the disease.
if [ -z "$python_bin" ]; then
	echo "[KUMA-CONN pre-commit] WARNING: no Python interpreter found." >&2
	echo "[KUMA-CONN pre-commit]          unit tests were NOT run." >&2
	exit 0
fi

if ! "$python_bin" -c "import pytest" >/dev/null 2>&1; then
	echo "[KUMA-CONN pre-commit] WARNING: pytest is not installed for '$python_bin'." >&2
	echo "[KUMA-CONN pre-commit]          unit tests were NOT run." >&2
	echo "[KUMA-CONN pre-commit]          install it once with: $python_bin -m pip install pytest" >&2
	exit 0
fi

# Note: pytest runs against the files on disk, not against the staged content.
# With unstaged changes in the working tree, what is verified here is not
# exactly what is being committed.
if [ -n "$(git diff --name-only -- '*.py' 2>/dev/null)" ]; then
	echo "[KUMA-CONN pre-commit] WARNING: unstaged Python changes detected." >&2
	echo "[KUMA-CONN pre-commit]          tests run against the working tree, not the staged snapshot." >&2
fi

if ! "$python_bin" -m pytest "$test_target"; then
	echo >&2
	echo "Commit blocked: unit tests failed." >&2
	echo "Reproduce with: python -m pytest tests/unit" >&2
	exit 1
fi

exit 0
