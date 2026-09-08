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

**Everything is configured in the admin panel, the API key included.** One
place, one person, no deployment change to make the plugin work. The cost is
stated rather than hidden: the core writes settings to `settings.json` in clear
text, so the key must be a read-only one and it must be rotated if that folder
is ever copied. See *Configuration* below.

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

Everything lives in one place: **Plugins → Uptime Kuma Connector → Settings** in
the Cheshire Cat admin panel. There is no environment variable to set, no file
to edit and no container to restart.

Three fields. Two are required to reach Uptime Kuma at all; the third is
optional and only matters when a monitor's name is not what users call it.

| Field | Required | Default | What it does |
| --- | --- | --- | --- |
| **Uptime Kuma: URL istanza** | yes | empty | The base URL of the instance, without `/metrics`. Empty disables the connector |
| **Uptime Kuma: API key** | yes | empty | A read-only key from the Uptime Kuma dashboard. Empty disables the connector |
| **Uptime Kuma: mappa alias** | no | empty | Maps what users say to monitor ids, one entry per line |

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

### Uptime Kuma: API key

Generate it in the Uptime Kuma dashboard, under **Settings → API Keys**, and
paste it here. The plugin authenticates the way Uptime Kuma expects, which is
not obvious: basic auth with an **empty username** and the key as the password.

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
  an exception and never its text, and never the request headers. A test asserts
  it.

### Uptime Kuma: mappa alias

Optional. It exists because `/metrics` exposes no tags, so the only things
linking what a user typed to a monitor are the monitor's **name** and its **id**.
Name matching is automatic and costs no upkeep; this field covers what no
heuristic can reach, a monitor called `srv-ugov-prod-01`.

One entry per line, aliases on the left of the colon and monitor ids on the
right, both comma-separated:

```
U-GOV, UGOV, Ugov: 12
Esse3, Segreteria online: 14, 15, 16
VPN: 17
```

Blank lines and lines starting with `#` are ignored. The id is the number in the
monitor's URL in Uptime Kuma, `/dashboard/<id>`; to check one, open
`<instance>/dashboard/<id>` and see whether the monitor that opens is the one
you meant.

**A malformed line is discarded on its own** — the field is never refused as a
whole, because one stray character must not disable the resolution of every
other service. The cost is that the panel reports nothing: the discarded line is
named in the log, and that is where to look when an alias does not work.

You usually need fewer entries than you would expect. Before matching, names and
queries are lowercased, their internal whitespace collapsed, and `-`, `.` and
`_` removed — which is why "UGOV" already finds `U-GOV - Autenticazione` without
an alias.

### What is deliberately not configurable

| Absent | Why |
| --- | --- |
| Request timeout | Fixed at 2 seconds. The call sits inside a conversation with a user waiting, which is a property of the situation and not a preference |
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
