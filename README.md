# Uptime Kuma Connector

A ***Cheshire Cat AI*** plugin that lets a help-desk assistant answer "is this
service up?" in real time, by querying an
***[Uptime Kuma](https://github.com/louislam/uptime-kuma)*** instance on
demand.

Without it, an assistant troubleshooting a user's device has no way to tell a
real outage from a local problem. This plugin gives it that signal, live,
without guessing.

## How it works

1. The user reports a service as unreachable, or asks whether it is down.
2. Cheshire Cat retrieves the `service_status()` tool and calls it with the
   service name the model infers from the message.
3. The plugin reads `GET <instance>/metrics` with the configured API key,
   within the configured deadline (2 seconds by default).
4. It resolves the requested service against the parsed monitors: by alias,
   then by exact name, then by containment — see *Resolving a service name*.
5. It returns one of four outcomes, each with a sentence that never invents a
   state.
6. The model combines that sentence with the support procedure from the
   knowledge base; the tool never answers the user directly.

### The four outcomes

| Outcome | When | What the model is told |
| --- | --- | --- |
| `known` | The service resolves to monitors with recognised statuses — up to five by name, up to ten through one alias | The service and the state of each check |
| `not_monitored` | No monitor matches | That no check exists — and that this is not the same as the service working |
| `ambiguous` | More monitors match than those ceilings allow, or the request names no service at all | That the request does not identify a service, and to ask which one |
| `unknown` | The connector is unreachable, unconfigured, unparseable, an alias points at a missing monitor, or a status code is unrecognised | That the state is not known — and not to conclude anything |

`unknown` is the outcome the whole design rests on: reporting a service as
healthy because its monitor is unreachable is worse than not checking at all,
so the sentence tells the model explicitly not to conclude anything.
`not_monitored` is a different, certain answer — "no check exists for that
service" — never a synonym for "we don't know."

## Resolving a service name

The model passes a service name such as `Portale Demo`; it never sees the
alias map. The plugin resolves it, in order:

1. an exact alias from the settings;
2. an exact monitor name;
3. containment either way — the query inside a monitor name, the name inside
   the query, or the same words in a different order.

Comparisons are case-insensitive, whitespace-collapsed, and read `-`, `.` and
`_` both as separators and as noise to remove — so `Portale-XX` matches
`Portale XX`, and `UGOV` matches `U-GOV - Autenticazione` with no alias
needed. Alias keys are folded the same way, but matching stays exact after
normalisation: `Portale Demo` never reaches `Portale Demo Test`.

Several matches are all reported, each with its own state, under neutral
ordinal labels (`controllo 1`, `controllo 2`, …) — external monitor names
never reach the model, only the log.

Bounds that keep untrusted text out of the answer and the log:

- a requested name is truncated to 80 characters;
- a monitor name over 160 characters, or containing control characters or
  something URL-shaped, is ignored;
- containment by substring needs at least three characters on both sides;
  shorter queries need a whole-word match;
- a monitor name that normalises to nothing is never matched heuristically
  (an explicit alias can still reach it).

## Design decisions

- **`/metrics`, not the status page** — the status-page endpoints require the
  page to be *published*, exposing service status publicly; `/metrics` needs
  only a read API key and carries id, name and status on one line.
- **No cache** — the status is read at the moment the question arrives; a
  forty-second-old answer is indistinguishable from a current one, so it is
  worse than none.
- **All four statuses stay distinct** (up, down, pending, maintenance) —
  collapsing them would report planned maintenance as a fault.
- **Resolution uses names and aliases, not tags** — `/metrics` exposes
  monitor tags too, but using them needs a naming convention agreed with
  whoever runs the instance; tracked as future work.

## Architecture

| File | Contents | Imports `cat`? |
| --- | --- | --- |
| `kuma_client.py` | All decision logic: URL, auth header, `/metrics` parsing, name resolution, the sentence for the model | **No** |
| `uptime_kuma_connector.py` | The Cheshire Cat adapter: settings, HTTP, the tool | Yes |
| `settings.py` | The admin settings model | Yes |
| `run-tests.py` | Runs the unit tier locally and the full suite in the container | No |
| `package-plugin.py` | Builds the release zip from an explicit file list | No |
| `tests/unit/` | The pure logic. Plain `pytest`, no Cheshire Cat | No |
| `tests/integration/` | Adapter behaviour, against a fake Cat and a mocked HTTP call | Yes |

The split keeps every decision testable without a running Cat, and keeps the
adapter thin enough to read.

## Requirements

**Cheshire Cat AI 1.9.2**, on the `1.x` line — the one core this plugin has
been run against, declared in `plugin.json` as `min_cat_version`. The core
reads that field into plugin metadata but never enforces it.

`httpx` only, declared in `requirements.txt` with no version constraint. The
plugin only uses long-stable httpx APIs (`Client`/`stream`,
`raise_for_status`, `iter_bytes`, the `timeout` parameter), so no specific
minimum is actually required — and since the core checks only package
presence, never version, a declared minimum would not be enforced anyway.

**`requirements.txt` carries no comments and no blank lines.** Cheshire Cat
calls `packaging.Requirement()` on each line inside a `try` that abandons the
whole list on the first failure — a comment there makes the core install
nothing, silently.

### Cheshire Cat prerequisite

`procedural_memory_k` controls how many tools Cheshire Cat retrieves per
message. With `k = 0`, `service_status` is never retrieved however well
configured it is. Keep it at `1` or more (core default: `3`). Worth
confirming before going live — nothing else about the plugin reveals a
`k = 0` misconfiguration.

## Installing and activating

The plugin folder is `uptime_kuma_connector`. Install it under
`cat/plugins/` — see the
[Cheshire Cat 1.x docs](https://cheshire-cat-ai.github.io/docs/1/) for how
your deployment does that, typically by placing the folder there or
uploading the zip built with `python package-plugin.py`.

1. Open the admin panel, **Plugins**, find **Uptime Kuma Connector**, switch
   it on.
2. Confirm activation in the core log: a line starting with
   `[uptime_kuma_connector] plugin activated`. No line means the plugin
   failed to load.
3. Configure it — an activated but unconfigured plugin answers every
   question with `unknown`.

## Configuration

**Plugins → Uptime Kuma Connector → Settings.** No environment variable, no
file, no restart.

| Field | Required | Default | What it does |
| --- | --- | --- | --- |
| **Uptime Kuma: URL istanza** | yes | empty | The instance base URL, without `/metrics`. Empty disables the connector |
| **Uptime Kuma: API key** | yes | empty | A read-only key from the Uptime Kuma dashboard. Empty disables the connector |
| **Uptime Kuma: mappa alias** | no | empty | Maps what users say to monitor ids, one entry per line |
| **Uptime Kuma: risposta massima (KiB)** | no | 1024 | Rejects a `/metrics` response above this size; range 64–10240 |
| **Uptime Kuma: timeout (secondi)** | no | 2 | Bounds the complete request, not each socket operation; range 1–10 |

Labels are in Italian because that is what the admin panel shows. **The
connector is enabled only when the URL and the key are both filled in.**

### Uptime Kuma: URL istanza

Just the instance root, e.g. `https://kuma.example.org` or
`http://uptime-kuma:3001` on a container network — the plugin appends
`/metrics` itself. Validated strictly: a trailing slash is stripped, a
sub-path is kept, query parameters, fragments and embedded credentials are
refused.

**HTTP is accepted**, with a log warning whenever the configuration
changes — the key travels as `base64(":" + key)`, an encoding, not
encryption. HTTPS with certificate verification disabled is never offered,
since it exposes the key while suggesting the channel is safe; for a private
CA, trust it in the container instead.

### Uptime Kuma: API key

Create it in Uptime Kuma under **Settings → API Keys**. Copy it
immediately — it is shown once. Paste it here.

- **Make it read-only** — this plugin never writes to Uptime Kuma.
- **It is stored in clear text** in `settings.json`. That file is
  git-ignored, but present in every backup or container snapshot of the
  plugin folder — rotate the key if that folder is ever copied.
- **It never reaches a log line** — on the call path the plugin logs the
  exception *type*, never its text, and never the request headers.

### Uptime Kuma: mappa alias

Optional. One entry per line: aliases left of the colon, monitor ids right
of it, both comma-separated.

```
# Administrative services
U-GOV, UGOV: 12
Esse3, Segreteria online: 14, 15, 16

# Remote access
VPN, GlobalProtect: 17
```

Blank lines and `#` comments are ignored. The id is the number in the
monitor's URL, `/dashboard/<id>`.

One alias may group up to ten monitor ids (an ordinary name match is capped
at five). A malformed line is discarded on its own — never disables the
field — and logged; a repeated id on one line collapses to one; a
conflicting alias keeps its first definition. Read the log when an alias
does not work: the panel accepts the field either way.

### What is deliberately not configurable

| Absent | Why |
| --- | --- |
| A cache or refresh interval | The status is read live, by design |
| A general on/off switch | An empty URL already disables the connector, and Cheshire Cat can deactivate the plugin |

## Testing

```bash
python run-tests.py --unit          # pure logic, local interpreter
python run-tests.py --integration   # adapter, inside the Cheshire Cat container
python run-tests.py                 # both
python run-tests.py --detailed      # ...listing every test name
```

The unit tier runs anywhere — it exercises `kuma_client.py`, which imports
nothing from `cat`. The integration tier needs the running core container,
since the adapter imports `cat` at import time; `run-tests.py` prints how to
start it when it is not running. Neither tier contacts a live Uptime Kuma
instance.

## Confirming the tool was used

The assistant's reply does not say which tool produced it — check the core
logs, filtered:

```powershell
docker compose logs --since 5m cheshire-cat-core 2>&1 |
  Select-String -Pattern '"action": "service_status"|intermediate_steps=.*service_status'
```

An `intermediate_steps` entry such as
`(('service_status', 'Portale demo'), ...)` confirms the question selected
the tool and used its result. A success line adds the outcome:

```text
[uptime_kuma_connector] monitoring endpoint request succeeded (outcome: known, resolution: alias).
```

`resolution: alias` means the settings map was used, `name` means automatic
name matching, and `none` means nothing matched. Every plugin log line
starts with `[uptime_kuma_connector]`.

### When it does not work, read the failure line

```text
[uptime_kuma_connector] could not read the monitoring endpoint (HTTPStatusError 401); the status is unknown.
```

| In the log | What it usually means |
| --- | --- |
| `HTTPStatusError 401` | The API key is wrong, expired, or revoked |
| `HTTPStatusError 404` | The URL does not point at an Uptime Kuma instance, or at a valid sub-path |
| `HTTPStatusError 301` / `307` | The instance redirects — **redirects are not followed on purpose**, so fix the configured URL to the one the instance actually serves |
| `ConnectError` / `ConnectTimeout` | The host is unreachable from the container |
| `ReadTimeout` | The instance is reachable but did not answer within the configured timeout |

Nothing else is logged on that path: not the URL, the headers, the body, or
the key.

## Limitations

- **`pending` and `maintenance` have never been seen in the wild.** They are
  implemented and unit-tested against Uptime Kuma's documented format, but
  no captured fixture contains them — only `up` and `down` have.
- **`/metrics` carries no timestamp** — the plugin cannot tell whether a
  value is a second or an hour old. A paused monitor was confirmed to
  disappear from `/metrics` rather than report a stale state.
- **Automatic name matching needs monitor names to contain the term users
  actually say** — where they don't, that is what the alias map is for.

## Out of scope

- **Any write towards Uptime Kuma**, a heartbeat included — this is a
  read-only consumer, and the credentials to do more are deliberately
  absent.
- Configuring or modifying Uptime Kuma, or authenticating to its dashboard
  over Socket.IO.
- Receiving webhooks — there is no cached state to keep fresh.
- Opening tickets, sending email, operational escalation.

## License

[GNU General Public License v3.0](LICENSE).
