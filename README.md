# Uptime Kuma Connector

A Cheshire Cat AI plugin that lets a help-desk assistant answer one question:
**is this service currently up?** It reads monitor state from an
[Uptime Kuma](https://github.com/louislam/uptime-kuma) instance, on demand, at
the moment the question is asked.

The point is to tell a service fault apart from a problem with the user's own
device — the most common question at a first-level help desk, and the one the
assistant currently has no data for. It answers with the right procedure to a
user whose actual problem is that the service is down.

> **Status: the first-version behaviour is implemented, its automated suite is
> green, and the repository is public.** The settings, pure client and
> `service_status` tool are wired. The adapter reads `/metrics` on demand with
> a two-second timeout and turns every failure into an explicit unknown
> result. Live end-to-end questions confirmed `up`, `down` and
> `not_monitored`. What remains — an interactive check of the alias map — is
> tracked in `ISSUES_TODO.md`, not here.

## How it is meant to work

1. The user reports that a service is unavailable or asks whether it is down.
2. Cheshire Cat retrieves the tool from procedural memory and calls
   `service_status()` with the service name inferred by the model.
3. The adapter loads the plugin settings and reads `GET <instance>/metrics`
   with the configured API key and a 2-second timeout.
4. The pure client parses only `monitor_status`, then resolves the requested
   service by configured alias, exact name, or normalised name/token containment,
   in that order. A monitor name that normalises to an empty value is never an
   heuristic match, but remains reachable through an explicit id-based alias.
   Names containing unsafe Unicode controls, URLs, or more than 160 characters
   are discarded before resolution.
5. The client returns one of `known`, `not_monitored`, `ambiguous`, or `unknown`,
   together with an Italian sentence that never invents a state.
6. The tool hands that fact back to the model; it does not answer directly. The
   model combines it with the procedure retrieved from the knowledge base.

A network, authentication or parsing failure produces `unknown`, never `up` or
`down`; see *Configuration* below for when no request is made at all.

### Who invokes the tool, and who uses the alias map

The **language model invokes the tool**: from the user's question it decides
whether `service_status` is relevant and passes a service name such as `Portale Demo`.
It does not read or interpret the alias map. The **plugin uses the map** after
the invocation, normalises the supplied name and resolves it in this order:

1. exact normalised alias from the settings;
2. exact normalised Uptime Kuma monitor name;
3. containment between the requested name and monitor names.

Alias matching is exact after case, whitespace, `-`, `.` and `_` normalisation;
it is not fuzzy or partial. For example, `Portale Demo: 42` matches
`Portale Demo`, but not `Portale Demo Test`. To accept both, configure
`Portale Demo, Portale Demo Test: 42`. An alias may also map
one service to several monitor ids. Once the plugin has read their current
states from `/metrics`, it returns a bounded factual sentence to the model,
which writes the final conversational answer.

The success log shows which path was used without exposing monitor names or
ids: `resolution: alias`, `resolution: name`, or `resolution: none`.

### Cheshire Cat prerequisite

`procedural_memory_k` (`k`) is the number of tools Cheshire Cat retrieves as
candidates for a user message. With `k = 0`, no tool is retrieved, so
`service_status` can never be called even when this plugin is enabled and
configured. Keep `k` at least `1`; the local instance uses `3`. Verify the
value on the target deployment before going live.

## Design decisions

Six choices the rest depends on.

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

**Everything is configured in the admin panel, the API key included.** One
place, one person, no deployment change to make the plugin work. The cost is
stated rather than hidden: the core writes settings to `settings.json` in clear
text, so the key must be a read-only one and it must be rotated if that folder
is ever copied. See *Configuration* below.

**All four Uptime Kuma statuses stay distinct**: down, up, pending, maintenance.
Collapsing them into up/down would announce planned maintenance as a fault, and
the user would open a ticket for scheduled work the assistant could have told
them about.

**The first version resolves a service name two ways.** Although `/metrics`
exposes tags, using them is deliberately deferred. The first version uses the
monitor name and the alias map in the plugin settings. Name matching costs no
upkeep; aliases cover names no heuristic can reach, such as
`srv-ugov-prod-01`. Several matches are all reported with their own state, using
neutral ordinal labels: external monitor names never enter the model context.

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
| `package-plugin.py` | Builds the release zip from an explicit runtime file list | No |
| `tests/unit/` | Pure logic. Plain `pytest`, no Cheshire Cat | No |
| `tests/integration/` | Adapter wiring, against the core as an interpreter | Yes |

## Out of scope

- **Any write towards Uptime Kuma**, heartbeat included: this is a read-only
  consumer.
- Receiving webhooks. A webhook keeps a cached state fresh; there is no cached
  state here, so it would serve no purpose.
- Configuring or modifying Uptime Kuma. The credentials to do so are
  deliberately absent.
- Authenticating to the dashboard over Socket.IO.
- Opening tickets, sending email, operational escalation.

## Installing and activating

The plugin folder is `uptime_kuma_connector`, matching the Python module and
the repository name. Cheshire Cat loads a plugin from a folder under
`cat/plugins/` on the instance; see the
[Cheshire Cat 1.x documentation](https://cheshire-cat-ai.github.io/docs/1/)
for how your deployment installs one — typically either by placing the folder
there directly, or by uploading the zip built by `python package-plugin.py`
(`dist/uptime_kuma_connector-<version>.zip`) through the admin panel.

Once the folder is in place:

1. Open the Cheshire Cat admin panel and go to **Plugins**.
2. Find **Uptime Kuma Connector** in the list and switch it on.
3. Confirm activation in the core log: it prints exactly one line, starting
   with `[uptime_kuma_connector] plugin activated`. No line means the plugin
   failed to load — check the log for the error instead of assuming it is
   running.
4. Continue with *Configuration* below: an activated but unconfigured plugin
   makes no network call and answers every question with `unknown`.

## Configuration

Everything lives in one place: **Plugins → Uptime Kuma Connector → Settings** in
the Cheshire Cat admin panel. There is no environment variable to set, no file
to edit and no container to restart.

Five fields. Two are required to reach Uptime Kuma, one maps aliases, and two
bound the time and memory consumed by a response.

| Field | Required | Default | What it does |
| --- | --- | --- | --- |
| **Uptime Kuma: URL istanza** | yes | empty | The base URL of the instance, without `/metrics`. Empty disables the connector |
| **Uptime Kuma: API key** | yes | empty | A read-only key from the Uptime Kuma dashboard. Empty disables the connector |
| **Uptime Kuma: mappa alias** | no | empty | Maps what users say to monitor ids, one entry per line |
| **Uptime Kuma: risposta massima (KiB)** | no | 1024 | Rejects `/metrics` responses above this size; accepted range 64–10240 KiB |
| **Uptime Kuma: timeout (secondi)** | no | 2 | Maximum duration of the complete request; accepted range 1–10 seconds |

**The connector is enabled only when the URL and the key are both filled in.**
One function decides it, and every path goes through that function — so there is
no state where the panel looks configured and the plugin quietly is not. With
either field empty no network call is attempted at all.

### Uptime Kuma: URL istanza

Just the instance root, for example `https://kuma.example.org` or
`http://uptime-kuma:3001`. The plugin appends `/metrics` itself.

Validated strictly, so a mistake is refused while you are still looking at the
form. A trailing slash is removed (joined with `/metrics` it would produce
`//metrics`, which some reverse proxies answer with a 404), a sub-path is kept
for an instance behind a reverse proxy, and query parameters, fragments and
credentials embedded in the URL are all refused.

**HTTP is accepted, and the log says so every time the configuration changes.**
The key travels in a Basic Auth header as `base64(":" + key)`, which is an
encoding and not encryption, so on plain HTTP anything that sees the traffic
sees the key. That is negligible on a container network — where the URL is just
the service name — and real across a campus LAN. HTTPS with certificate
verification disabled is not offered: it exposes the key exactly as HTTP does
while suggesting the channel is protected.

### Creating the API key on Uptime Kuma

Assuming Uptime Kuma is already installed, running and configured with the
monitors to expose:

1. Log into the Uptime Kuma dashboard and open **Settings → API Keys**.
2. Click **Add API Key**, give it a name (e.g. `uptime_kuma_connector`), and
   optionally set an expiry date.
3. Copy the generated key immediately — Uptime Kuma shows it only once, at
   creation time, and cannot display it again afterwards.
4. Paste it into this plugin's **Uptime Kuma: API key** field, below.

### Uptime Kuma: API key

The key created above, pasted as is. The plugin authenticates the way Uptime
Kuma expects, which is not obvious: basic auth with an **empty username** and
the key as the password.

Only whitespace is stripped from what you paste — a trailing newline from a
copy-paste would otherwise travel inside the header and turn every call into a
401 that looks like a wrong key. Nothing else is validated, because Uptime Kuma
documents neither the length nor the character set of its keys, so the instance
answering 401 is the only honest test of whether a key is right.

Three things worth knowing before you paste one:

- **Make it read-only.** This plugin never writes to Uptime Kuma, and a key with
  more rights than that buys nothing and risks more.
- **It is stored in clear text**, in `settings.json` inside the plugin folder,
  and the panel shows it as ordinary text. That file is in `.gitignore`, so it
  stays out of the repository — but it is in every backup, container snapshot
  and support copy of that folder. Rotate the key if the folder is ever copied.
- **It never reaches a log line.** On the call path the plugin logs the type of
  an exception and never its text, and never the request headers or the
  instance URL. Each configured request logs its start at `INFO`; a completed
  request logs its outcome and resolution source (`alias`, `name`, or `none`),
  while a failed request remains a `WARNING`. Every plugin log starts with
  `[uptime_kuma_connector]`. Tests
  assert these boundaries. This guarantee covers the credential only: monitor
  **names** do reach a `WARNING` line on purpose — see below.

### Uptime Kuma: mappa alias

Optional. The first version does not use the tags exposed by `/metrics`, so it
links what a user typed to a monitor through the monitor's **name** or an
explicit **id** in this map. Name matching is automatic and costs no upkeep;
this field covers what no heuristic can reach, a monitor called
`srv-ugov-prod-01`.

One entry per line, aliases on the left of the colon and monitor ids on the
right, both comma-separated:

```
# Administrative services
U-GOV, UGOV: 12
Esse3, Segreteria online: 14, 15, 16

# Remote access
VPN, GlobalProtect: 17

# Mail service
Posta, Email, Webmail: 21
```

The first line maps several ways of writing the same service to monitor `12`.
The `Esse3` line deliberately maps one service to three monitors, so the tool
can report the state of all its components. Comments can be used to organise a
longer map.

**One alias can group up to 10 monitor ids.** Above that the tool reports the
service as `ambiguous` instead of naming ten-plus components in one sentence.
An ordinary name match is capped at 5 for the same reason, at a lower number
because an unintentional name collision is a weaker signal than an alias an
administrator wrote on purpose.

Blank lines and lines starting with `#` are ignored. The id is the number in the
monitor's URL in Uptime Kuma, `/dashboard/<id>`; to check one, open
`<instance>/dashboard/<id>` and see whether the monitor that opens is the one
you meant.

**A malformed line is discarded on its own** — the field is never refused as a
whole, because one stray character must not disable the resolution of every
other service. The cost is that the panel reports nothing: the discarded line is
named in the log, and that is where to look when an alias does not work.

**A request that matches no monitor also logs, at `WARNING`, the requested name
and up to three of the closest monitor names** — the only channel an
administrator has for spotting a typo in an alias or a name nobody wrote an
alias for. Those names never reach the sentence returned to the model or the
user; the log is the one place they appear.

You usually need fewer entries than you would expect. Before matching, names and
queries are lowercased, their internal whitespace collapsed, and `-`, `.` and
`_` removed — which is why "UGOV" already finds `U-GOV - Autenticazione` without
an alias. A query shorter than three characters only matches a monitor name by
whole word, never by substring, so a single letter cannot accidentally match
every name that happens to contain it.

### Network safety limits

The response ceiling is expressed in KiB and defaults to 1024 (one MiB). The
timeout defaults to two seconds and applies to the complete request, not only to
each socket operation. Exceeding either limit returns the explicit `unknown`
sentence and logs only the failure type; the URL, response body and credentials
remain absent from logs.

### What is deliberately not configurable

| Absent | Why |
| --- | --- |
| Cache or refresh interval | There is none. The status is read at the moment the question arrives, because a forty-second-old status is indistinguishable from a current one |
| A general on/off switch | An empty URL disables the connector, and Cheshire Cat already has one — deactivating the plugin. A second switch could disagree with the first |

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

Run the full suite, optionally listing every test name, with:

```bash
python run-tests.py
python run-tests.py --detailed
```

The runner reports how to start `cheshire-cat-core` if the container is not
running.

The unit tier covers parsing, aliases, resolution, all response outcomes, and
the packaging file list. The integration tier covers settings, validators,
tool wiring and adapter behaviour with a fake Cat and mocked HTTP. Neither
tier contacts a live Uptime Kuma instance.

## Verifying that a question invoked the tool

The chatbot answer alone is not proof. After asking the question, filter the
core logs instead of sharing their raw output, which may contain session tokens:

```powershell
docker compose logs --since 5m cheshire-cat-core 2>&1 |
  Select-String -Pattern '"action": "service_status"|intermediate_steps=.*service_status'
```

An `action` followed by an `intermediate_steps` entry such as
`(('service_status', 'Portale demo'), ...)` confirms that the question selected
the tool, passed it the service name and used its result.

The plugin's success line also identifies how the service was resolved without
exposing its monitor name or id:

```text
[uptime_kuma_connector] monitoring endpoint request succeeded (outcome: known, resolution: alias).
```

`resolution: alias` confirms the settings map was used; `name` means automatic
name matching, and `none` means no monitor matched.

## Before this can be published

All four checks below are done; kept here as a record rather than removed,
since the first closed as a documented limitation rather than a completed
capture — recorded plainly rather than left open-ended, per
`ISSUES_RESOLVED.md`.

1. ~~Capture a complete `/metrics` line while a monitor is pending or in
   maintenance.~~ Closed 2026-09-09: `0` and `1` are captured and
   fixture-backed. `2` was only ever observed transiently, without a complete
   line to capture; `3` is **not verifiable on this instance at all**. Revisit
   only if a different instance becomes available.
2. ~~Verify the [procedural-memory prerequisite](#cheshire-cat-prerequisite) on
   the target instance.~~ Checked 2026-09-09: `k = 3` on both the local and
   the production deployment.
3. ~~Choose a licence and add the file — the registry requires open source.~~
   Done: [GNU GPLv3](LICENSE).
4. ~~Choose and verify the release version, run `python package-plugin.py` to
   build `dist/uptime_kuma_connector-<version>.zip`, and publish the
   repository so the registry thumbnail resolves.~~ Done: version `0.0.3`,
   the repository is public.

## License

[GNU General Public License v3.0](LICENSE).
