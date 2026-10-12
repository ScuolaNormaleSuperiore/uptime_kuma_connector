# Changelog

Notable changes to `Uptime Kuma Connector`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.3] - 2026-10-12
- Fixed: a generic word around a service name (`programma Planning`, `portale Esse3`) no longer hides the monitor; Italian and English generic words are dropped in a last matching pass, logged as `resolution: fallback`
- Fixed: a monitor name is no longer matched inside another word (`Mail` for `Gmail`); containment is on whole words, and brackets and slashes count as word boundaries
- Fixed: an alias written with `https://` in the alias map is no longer discarded
- Fixed: a response with no readable monitor line is no longer logged as a success, and no longer as "no monitor matches"
- Added `DOC/MATCH_SERVIZI.md`, a guide to how a request is matched to a monitor
- Updated the documentation

## [1.0.2] - 2026-10-07
- The tests and the development scripts moved to hidden folders (`.tests/`, `.tools/`), so Cheshire Cat no longer imports them
- Added a layout test that fails when the Cat would import a file the plugin does not need
- Updated the documentation and the test commands
- The closest-name diagnostic on a `not_monitored` answer compares at most 200 names, so it no longer adds latency on large instances
- The shared HTTP client is closed when the plugin is deactivated
- Workers stuck in name resolution are capped at eight; past that a call answers `unknown` at once

## [1.0.1] - 2026-09-23
- AI Code Review and bug-fixing

## [1.0.0] - 2026-09-22
- AI Code Review
- Bug-fixing: Fixed bugs and security issues
- Updated the documentation
  