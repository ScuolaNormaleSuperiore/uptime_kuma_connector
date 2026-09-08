#!/usr/bin/env bash
set -euo pipefail

echo "[KUMA-CONN pre-commit] Running secret scan..." >&2

if ! command -v git >/dev/null 2>&1; then
	exit 0
fi

if git rev-parse --verify HEAD >/dev/null 2>&1; then
	diff_output="$(git diff --cached --unified=0 --no-color --diff-filter=ACMRTUXB)"
else
	# Initial commit: compare against empty tree.
	empty_tree="$(git hash-object -t tree /dev/null)"
	diff_output="$(git diff --cached --unified=0 --no-color --diff-filter=ACMRTUXB "$empty_tree")"
fi

if [ -z "$diff_output" ]; then
	exit 0
fi

# No path is excluded from the scan. This plugin has no generated or vendored
# text assets, and the artifacts it does produce (__pycache__/, .pytest_cache/)
# belong in .gitignore, so they never reach the staging area.
added_lines="$(printf '%s\n' "$diff_output" | awk '
	/^\+\+\+ / { next }
	/^\+/ {
		sub(/^\+/, "", $0)
		print
	}
')"

if [ -z "$added_lines" ]; then
	exit 0
fi

# There is deliberately **no exemption mechanism**, per line or per path. An
# escape hatch in a secret scan is a way to silence a real detection. If a
# legitimate need appears — a fixture, an example in documentation — add it per
# line rather than per path, so the exemption sits next to the value it covers
# and is visible in the diff a reviewer reads, and add a test proving it applies
# to that one line only.
#
# High-signal patterns only, to keep false positives rare enough that nobody
# reaches for `--no-verify`.
#
# Two details are load-bearing and were both real defects in the sibling plugin,
# found by tests written for its hooks:
#
#   * `grep -e` is not optional. The private-key pattern starts with `-----`, so
#     without `-e` grep reads it as options and dies; with `2>/dev/null` and
#     `|| true` around it, the most serious pattern in the list then reported
#     nothing and every commit carrying a private key passed.
#   * the last pattern is quoted with $'...' and not '...'. `\x27` is an ANSI-C
#     escape that bash expands only inside $'...'; in ordinary single quotes the
#     character class meant {" \ x 2 7} instead of {" '}, so single-quoted values
#     were never matched and any value containing x, 2 or 7 broke the match.
#
# No verbatim example is written here on purpose: this file is scanned by the
# very patterns it defines.
patterns=(
	'-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----'
	'(AKIA|ASIA)[A-Z0-9]{16}'
	'gh[pousr]_[A-Za-z0-9]{36,255}'
	'github_pat_[A-Za-z0-9_]{20,}'
	'xox[baprs]-[A-Za-z0-9-]{10,}'
	'sk-[A-Za-z0-9]{20,}'
	# The API key of the Uptime Kuma instance: the one credential this plugin
	# handles, read from UPTIME_KUMA_API_KEY and deliberately absent from the
	# settings model.
	#
	# It leaks in a shape none of the generic patterns sees, because it is
	# neither an assignment nor a path segment: it travels **base64-encoded
	# inside an Authorization header**, by the convention of Uptime Kuma — basic
	# auth with an empty username and the key as the password. The two forms it
	# takes when somebody reproduces a call while debugging are a `Basic` header
	# and a `curl -u` invocation, so both are matched.
	#
	# The patterns describe the *carrier* and not the value, deliberately. Uptime
	# Kuma does not document the key's length or character set as a contract, so
	# a pattern built on a guessed alphabet would be the kind that silently stops
	# matching — the failure mode that matters most in a secret scan.
	#
	# The bound of 16 base64 characters keeps the word `Basic` in prose from
	# matching: base64 of a key short enough to fall below it would not be a key
	# worth protecting.
	$'[Aa]uthorization[[:space:]]*:?[[:space:]]*["\x27]?Basic[[:space:]]+[A-Za-z0-9+/=]{16,}'
	'curl[[:space:]][^;|]*[[:space:]]-(u|-user)[[:space:]]+[^[:space:]]{4,}'
	# Kept from the dropped push design: it costs nothing and still catches a
	# paste from that earlier version or from an unrelated Uptime Kuma note.
	'/api/push/[A-Za-z0-9]{6,}'
	'[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]]+:[^[:space:]]+@[^[:space:]]+'
	$'(api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|push[_-]?token|api[_-]?secret|password|passwd|pwd|secret)[[:space:]]*[:=][[:space:]]*["\x27][^"\x27]{8,}["\x27]'
)

found=0
for pattern in "${patterns[@]}"; do
	matches="$(grep -E -i -n -e "$pattern" <<< "$added_lines" 2>/dev/null || true)"
	if [ -n "$matches" ]; then
		if [ "$found" -eq 0 ]; then
			echo "[KUMA-CONN pre-commit] Potential secret detected in staged changes:" >&2
			found=1
		fi
		printf '%s\n' "$matches" >&2
	fi

done

if [ "$found" -eq 1 ]; then
	echo >&2
	echo "Commit blocked. Remove secrets or move safe examples outside staged changes." >&2
	echo "If this is a false positive, adjust the pattern list in .githooks/check-staged-secrets.sh." >&2
	exit 1
fi

exit 0
