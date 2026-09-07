#!/usr/bin/env bash
set -euo pipefail

echo "[KUMA-CONN pre-commit] Checking staged Python syntax..." >&2

if ! command -v git >/dev/null 2>&1; then
	exit 0
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

python_files="$(git diff --cached --name-only --diff-filter=ACMR | grep '\.py$' || true)"
if [ -z "$python_files" ]; then
	exit 0
fi

python_bin=""
for candidate in python python3 py; do
	if command -v "$candidate" >/dev/null 2>&1; then
		python_bin="$candidate"
		break
	fi
done

if [ -z "$python_bin" ]; then
	echo "[KUMA-CONN pre-commit] WARNING: no Python interpreter found." >&2
	echo "[KUMA-CONN pre-commit]          syntax check was NOT run." >&2
	exit 0
fi

# A syntax error in this plugin is worse than usual: the core runs with
# auto-reload, so writing a broken file restarts it and it stays down until the
# file is fixed. This gate is the cheapest place to catch that.
while IFS= read -r file; do
	if ! "$python_bin" -m py_compile "$file"; then
		echo >&2
		echo "Commit blocked: Python syntax check failed for $file." >&2
		exit 1
	fi
done <<< "$python_files"

exit 0
