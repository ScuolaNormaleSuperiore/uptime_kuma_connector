# Uptime Kuma Connector

A Cheshire Cat AI plugin that lets a help-desk assistant answer one question:
**is this service currently up?** It reads monitor state from an
[Uptime Kuma](https://github.com/louislam/uptime-kuma) instance, on demand, at
the moment the question is asked.

The point is to tell a service fault apart from a problem with the user's own
device — the most common question at a first-level help desk, and the one the
assistant currently has no data for. It answers with the right procedure to a
user whose actual problem is that the service is down.

> **Status: the configuration works, the behaviour does not.** The settings are
> declared, validated and read; no tool is registered, no hook is registered,
> and no request is made to Uptime Kuma yet.
>
> The specification to rebuild it from is
> [`DOC/Specifiche.md`](DOC/Specifiche.md), in Italian.

## How it is meant to work

```
user:   "I can't connect to the VPN, is it just me?"
model:  calls service_status("VPN")
tool:   GET <instance>/metrics   (basic auth, 2 s timeout)
tool:   -> "Il servizio VPN - GlobalProtect risulta attivo."
model:  merges that fact with the procedure retrieved from the knowledge base
```

The tool does not answer the user: it hands a fact to the model, which combines
it with what it retrieved.

## Design decisions

Five choices the rest depends on. Each is argued in full in the specification;
this is the summary.

**It reads `/metrics`, not the status page.** Uptime Kuma has no general REST
API — the dashboard talks Socket.IO after authenticating. Of the public
endpoints, `/metrics` carries the monitor name, its id and its status on a
single line, so there is no second request and no join to get wrong. The
decisive reason is different though: the status-page endpoints work only if the
status page is **published**, which for an internal ICT help desk means exposing
service status to the world. The cost is an API key.

**The status is read in real time, with no cache.** A dashboard needs cached
state because it redraws constantly; a chatbot asks once, when someone asks, and
the answer has to be true *then*. A forty-second-old status is worse than none,
because it is indistinguishable from a current one.

**The API key comes from the environment**, `UPTIME_KUMA_API_KEY`, and there is
no settings field for it — the core persists settings to `settings.json` in
clear text, and this key grants read access to the whole monitoring system.

**All four Uptime Kuma statuses stay distinct**: down, up, pending, maintenance.
Collapsing them into up/down would announce planned maintenance as a fault, and
the user would open a ticket for scheduled work the assistant could have told
them about.

**A service name is resolved two ways.** `/metrics` exposes no tags, so the only
correlation data is the monitor id and its name — which means the link between
"U-GOV" and the right monitor can live in exactly two places: the monitor name
inside Kuma, or this plugin's settings. Both are used. Name matching costs no
upkeep and covers the normal case; an alias map in the settings reaches what no
heuristic can, a monitor called `srv-ugov-prod-01`. When a request matches
several monitors, all of them are reported with their own state — picking one
would be a guess, listing them tells a help desk which component is failing.

**Four outcomes, never two.** `known`, `not_monitored`, `ambiguous`, and
`unknown`, where the last two carry the weight of the design. A check that
reports a service as healthy because its monitor is unreachable is worse than no
check: the user takes the false reassurance as being about the service, not about
the tool. The sentence for that case explicitly tells the model not to conclude
anything, because a model asked "is the VPN down?" fills a silence if it is
allowed to. And `not_monitored` is a separate answer, not a synonym: "no check
exists for that service" is certain, "I do not know" is our own malfunction.

The invariant, in four words: **never invent a state.**

## Architecture

| File | Contents | Imports `cat`? |
| --- | --- | --- |
| `kuma_client.py` | All decision logic: URL, auth header, `/metrics` parsing, name resolution, the sentence for the model | **No** |
| `uptime_kuma_connector.py` | The Cheshire Cat adapter: settings, HTTP, the tool | Yes |
| `settings.py` | The admin settings model | Yes |
| `run-tests.py` | Test runner for local unit tests and container integration tests | No |
| `tests/unit/` | Pure logic. Plain `pytest`, no Cheshire Cat | No |
| `tests/integration/` | Adapter wiring, against the core as an interpreter | Yes |
| `DOC/Specifiche.md` | The specification, in Italian | — |


## Out of scope

- **Any write towards Uptime Kuma**, heartbeat included: this is a read-only
  consumer.
- Receiving webhooks. A webhook keeps a cached state fresh; there is no cached
  state here, so it would serve no purpose.
- Configuring or modifying Uptime Kuma. The credentials to do so are
  deliberately absent.
- Authenticating to the dashboard over Socket.IO.
- Opening tickets, sending email, operational escalation.

## Configuration

`Plugins → Uptime Kuma Connector → Settings`

| Field | Default | Notes |
| --- | --- | --- |
| Instance URL | empty | Empty disables the connector. HTTPS preferred; HTTP is accepted and warned about in the log, because the instance may sit on a private network |
| Alias map | empty | Optional, one entry per line: `alias, alias: id, id` |

When implemented: the instance URL, and an optional alias map for the cases
where a monitor name is not enough — one entry per line, `alias, alias: id, id`.
The API key is **not** a setting; it comes from `UPTIME_KUMA_API_KEY`.

## Requirements

`httpx` only, declared in `requirements.txt` with a permissive range and ahead
of first use: `httpx` ships in the core image but is not declared by the core,
so nothing promises it, and a dependency missing at activation time is the
expensive failure.

**That file carries no comments and no blank lines.** Cheshire Cat does not hand
it to pip: it calls `packaging.Requirement()` on every line, inside a `try` that
abandons the whole loop on the first failure. A comment is valid for pip and
fatal here — it makes the core install *no* dependency at all, logging one error
while activation continues, so the plugin works on a machine that already has
the packages and fails at import on a clean one.

## Testing

Unit tests use the local Python interpreter:

```bash
python run-tests.py --unit
```

Integration tests need the core importable and run inside the Cheshire Cat
container:

```bash
python run-tests.py --integration
```

Run the full suite with `python run-tests.py`. Add `--detailed` to any command
to list every test name. The runner reports how to start `cheshire-cat-core` if
the container is not running.

What the current tests check is not behaviour — there is none — but the
invariants of an empty plugin: that the pure module imports nothing from `cat`,
that the outcomes and the four statuses stay distinct, that the settings
form is empty, and that **nothing is registered that is not implemented**. That
last one matters: a skeleton exposing a tool would let the model call it and get
nothing back.

## Before this can be published

1. Confirm the shape of `/metrics` against a real instance, and whether
   `monitor_status` emits `2` and `3` as well as `0` and `1`.
2. Check that procedural memory is enabled on the target instance. Tools live
   there, and with `k = 0` the tool is never retrieved and never invoked.
3. Choose a licence and add the file — the registry requires open source.
4. Land the initial commit and push it, after `git config core.hooksPath
   .githooks`: the repository at
   [ScuolaNormaleSuperiore/uptime_kuma_connector](https://github.com/ScuolaNormaleSuperiore/uptime_kuma_connector)
   exists and has no commit yet.
5. Add `logo.png` — `plugin.json` declares a `thumb` that points at it.
