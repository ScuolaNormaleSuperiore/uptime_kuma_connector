# Uptime Kuma Connector

A Cheshire Cat AI plugin that lets a help-desk assistant answer one question:
**is this service currently up?** It reads monitor state from an
[Uptime Kuma](https://github.com/louislam/uptime-kuma) instance on demand, at
the moment the question is asked.

The point is to tell a service fault apart from a problem with the user's own
device — the most common question at a first-level help desk, and the one an
assistant usually has no data for. Without it, the assistant answers with the
right procedure to a user whose actual problem is that the service is down.

> **Status:** the first version is implemented and its automated suite is green.
> Live questions against a real instance confirmed the `up`, `down` and
> `not_monitored` answers. `pending` and `maintenance` are implemented but have
> never been observed on the instance available for testing — see *Limitations*.

## How it works

1. The user reports that a service is unavailable, or asks whether it is down.
2. Cheshire Cat retrieves the tool from procedural memory and calls
   `service_status()` with the service name inferred by the model.
3. The plugin reads `GET <instance>/metrics` with the configured API key, within
   the configured request deadline — two seconds by default.
4. It parses the `monitor_status` lines and resolves the requested service: by
   alias, then by exact name, then by containment. See *Resolving a service
   name*.
5. It returns one of four outcomes, with an Italian sentence that never invents
   a state.
6. The tool hands that sentence to the model rather than answering the user. The
   model combines it with the procedure retrieved from the knowledge base.

### The four outcomes

| Outcome | When | What the model is told |
| --- | --- | --- |
| `known` | The service resolves to monitors with recognised statuses — up to five by name, up to ten through one alias | The service, and the state of each check |
| `not_monitored` | No monitor matches | That no check exists, **and that this does not mean the service works** |
| `ambiguous` | More monitors match than those ceilings allow | That the request does not identify a service, and to ask the user which one |
| `unknown` | Five cases, below | That the state is not known, **and not to conclude anything** |

`unknown` is the outcome the design rests on, and it is never silence. A check
that reports a service as healthy because its monitor is unreachable is worse
than no check at all: the user takes the false reassurance as being about the
service rather than about the tool. Its sentence therefore tells the model
explicitly not to conclude anything — a model asked "is the VPN down?" fills a
silence if it is allowed to.

Five things produce it:

- the connector is not configured, in which case no request is made at all;
- the instance is unreachable, refuses the key, or does not answer in time;
- the response cannot be parsed, or is larger than the configured ceiling;
- an alias points only at monitor ids absent from `/metrics`. That service *is*
  configured as monitored, so the configuration is what is broken, and answering
  "not monitored" would put the blame in the wrong place;
- a monitor reports a status code outside the four known ones. An unrecognised
  code is never mapped onto `down` for convenience.

And `not_monitored` is a separate answer, never a synonym: "no check exists for
that service" is certain, while "I do not know" is our own malfunction.

The invariant, in four words: **never invent a state.**

## Resolving a service name

The **model invokes the tool** and passes it a service name such as
`Portale Demo`; it never reads the alias map. The **plugin resolves that name**
against what `/metrics` returned, in this order:

1. an exact alias from the settings;
2. an exact monitor name;
3. containment either way — the query inside a monitor name, a monitor name
   inside the query, or the same words in a different order.

All three comparisons are made on a normalised form: lowercased, internal
whitespace collapsed, and `-`, `.` and `_` removed. That is why `UGOV` finds
`U-GOV - Autenticazione` with no alias at all, and it removes a whole class of
aliases nobody then has to write.

Alias matching is exact after that normalisation, not fuzzy: `Portale Demo: 42`
matches `Portale Demo` but not `Portale Demo Test`. To accept both, write
`Portale Demo, Portale Demo Test: 42`.

Several matches are all reported, each with its own state, under neutral ordinal
labels — external monitor names never enter the model context. Picking one would
be a guess, and naming all of them tells a help desk *which* component is
failing.

Four bounds keep untrusted text out of the answer and out of the logs:

- the requested name is truncated to 80 characters, since it reappears in the
  sentence handed to the model;
- a monitor name is ignored if it exceeds 160 characters, or contains Unicode
  control characters or something shaped like a URL;
- containment by substring requires at least three characters **on both sides**.
  Below that only a whole-word match counts, so a single letter cannot match
  every name that happens to contain it;
- a monitor name that normalises to nothing is never matched heuristically,
  although an explicit id-based alias still reaches it.

## Design decisions

**It reads `/metrics`, not the status page.** Uptime Kuma has no general REST
API — the dashboard talks Socket.IO after authenticating. Of the public
endpoints, `/metrics` carries a monitor's name, id and status on one line, so
there is no second request and no join to get wrong. The decisive reason is
different though: the status-page endpoints work only if the status page is
**published**, which for an internal help desk means exposing service status to
the world. The cost is an API key.

**The status is read in real time, with no cache.** A dashboard needs cached
state because it redraws constantly; a chatbot asks once, when someone asks, and
the answer has to be true *then*. A forty-second-old status is worse than none,
because it is indistinguishable from a current one.

**Everything is configured in the admin panel, the API key included.** One
place, one person, no deployment change to make the plugin work — at the cost
stated under *Uptime Kuma: API key*.

**All four Uptime Kuma statuses stay distinct**: up, down, pending, maintenance.
Collapsing them into up/down would announce planned maintenance as a fault, and
the user would open a ticket for scheduled work the assistant could have told
them about.

**A name is resolved two ways, not three.** `/metrics` also exposes monitor
tags, and using them is deliberately deferred: it would need a tag naming
convention agreed with whoever runs the instance. Automatic name matching costs
no upkeep, and the alias map covers what no heuristic can reach — a monitor
called `srv-ugov-prod-01`.

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

`httpx` only, declared in `requirements.txt` with a permissive range. It ships
in the Cheshire Cat image but is not declared by the core, so nothing promises
it, and a dependency missing at activation time is the expensive failure.

**That file deliberately carries no comments and no blank lines.** Cheshire Cat
does not hand it to pip: it calls `packaging.Requirement()` on each line, inside
a `try` that abandons the whole list at the first failure. A comment is valid
for pip and fatal here — the core would install nothing at all, logging one
error while activation continued.

### Cheshire Cat prerequisite

Tools live in procedural memory, and `procedural_memory_k` is how many of them
Cheshire Cat retrieves as candidates for a message. With `k = 0` no tool is
retrieved, so `service_status` can never be called however well configured it is
— the failure that looks exactly like a plugin doing nothing. Keep it at `1` or
more; the core default is `3`. Worth confirming on a deployment before going
live, because nothing else about the plugin reveals it.

## Installing and activating

The plugin folder is `uptime_kuma_connector`, matching the Python module and the
repository name. Cheshire Cat loads a plugin from a folder under `cat/plugins/`;
see the [Cheshire Cat 1.x documentation](https://cheshire-cat-ai.github.io/docs/1/)
for how your deployment installs one — typically by placing the folder there, or
by uploading the zip built with `python package-plugin.py`
(`dist/uptime_kuma_connector-<version>.zip`) through the admin panel.

Then:

1. Open the admin panel, go to **Plugins**, find **Uptime Kuma Connector** and
   switch it on.
2. Confirm activation in the core log: it prints one line starting with
   `[uptime_kuma_connector] plugin activated`. No line means the plugin failed
   to load — look for the error rather than assuming it is running.
3. Configure it. An activated but unconfigured plugin makes no network call and
   answers every question with `unknown`.

## Configuration

Everything lives in **Plugins → Uptime Kuma Connector → Settings**. There is no
environment variable to set, no file to edit and no container to restart.

| Field | Required | Default | What it does |
| --- | --- | --- | --- |
| **Uptime Kuma: URL istanza** | yes | empty | The instance base URL, without `/metrics`. Empty disables the connector |
| **Uptime Kuma: API key** | yes | empty | A read-only key from the Uptime Kuma dashboard. Empty disables the connector |
| **Uptime Kuma: mappa alias** | no | empty | Maps what users say to monitor ids, one entry per line |
| **Uptime Kuma: risposta massima (KiB)** | no | 1024 | Rejects a `/metrics` response above this size; range 64–10240 |
| **Uptime Kuma: timeout (secondi)** | no | 2 | Bounds the complete request, not each socket operation; range 1–10 |

The labels are quoted in Italian because that is what the panel shows.

**The connector is enabled only when the URL and the key are both filled in**,
and one function decides it, so there is no state where the panel looks
configured and the plugin quietly is not.

### Uptime Kuma: URL istanza

Just the instance root — `https://kuma.example.org`, or
`http://uptime-kuma:3001` on a container network. The plugin appends `/metrics`
itself.

It is validated strictly, so a mistake is refused while you are still looking at
the form. A trailing slash is removed (joined with `/metrics` it would produce
`//metrics`, which some reverse proxies answer with a 404); a sub-path is kept,
for an instance behind a reverse proxy; query parameters, fragments and
credentials embedded in the URL are refused.

**HTTP is accepted, and the log says so whenever the configuration changes.**
The key travels in a Basic Auth header as `base64(":" + key)`, which is an
encoding and not encryption, so on plain HTTP anything that sees the traffic
sees the key. That is negligible on a container network and real across a campus
LAN. HTTPS with certificate verification disabled is *not* offered: it exposes
the key exactly as HTTP does while suggesting the channel is protected. For an
instance behind a private CA, trust that CA in the container instead.

### Uptime Kuma: API key

Create it in the Uptime Kuma dashboard under **Settings → API Keys**: *Add API
Key*, a name such as `uptime_kuma_connector`, an optional expiry date. **Copy it
immediately** — Uptime Kuma shows it once, at creation time, and cannot display
it again. Then paste it into this field.

The plugin authenticates the way Uptime Kuma expects, which is not obvious:
basic auth with an **empty username** and the key as the password. Only
whitespace is stripped from what you paste — a trailing newline would otherwise
travel inside the header and turn every call into a 401 that looks like a wrong
key. Nothing else is validated, because Uptime Kuma documents neither the length
nor the character set of its keys, so the instance answering 401 is the only
honest test.

Three things worth knowing before you paste one:

- **Make it read-only.** This plugin never writes to Uptime Kuma; more rights
  buy nothing and risk more.
- **It is stored in clear text**, in `settings.json` inside the plugin folder,
  and the panel shows it as ordinary text. That file is git-ignored, so it stays
  out of the repository — but it is in every backup, container snapshot and
  support copy of that folder. Rotate the key if the folder is ever copied.
- **It never reaches a log line.** On the call path the plugin logs the *type* of
  an exception and never its text, and never the request headers, the instance
  URL or the response body. Tests assert these boundaries.

### Uptime Kuma: mappa alias

Optional, and the way to reach a monitor whose name no heuristic will ever
connect to what users say. One entry per line: aliases left of the colon,
monitor ids right of it, both comma-separated.

```
# Administrative services
U-GOV, UGOV: 12
Esse3, Segreteria online: 14, 15, 16

# Remote access
VPN, GlobalProtect: 17
```

The first line maps two spellings of one service to monitor `12`. The `Esse3`
line deliberately groups three monitors under one name, so the tool can report
the state of every component. Blank lines and `#` comments are ignored.

The id is the number in the monitor's URL in Uptime Kuma, `/dashboard/<id>`. To
check one, open `<instance>/dashboard/<id>` and see whether the monitor that
opens is the one you meant — for an operator that is the only simple way to
catch a wrong number.

**One alias may group up to ten monitor ids**, above which the service is
reported as `ambiguous` rather than naming ten-plus components in one sentence.
An ordinary name match is capped at five, lower because an accidental name
collision is a weaker signal than a grouping an administrator wrote on purpose.

Three parsing rules, each keeping one mistake from spreading:

- **A malformed line is discarded on its own** and never refuses the field as a
  whole, because one stray character must not disable the resolution of every
  other service. One bad id discards its own line entirely rather than applying
  half an alias.
- **A repeated id on one line collapses to one.** A copy-paste duplicate is not
  a second component to report twice.
- **A conflicting alias keeps the first definition**, and the later line is
  ignored, so the order of the lines never becomes a hidden rule.

Each of those is logged, and the log is the only place they appear: the panel
accepts the field either way. That is the declared cost of having no global
failure point — when an alias does not work, read the log.

Two `WARNING` lines exist for exactly that:

- a request that matched nothing logs the requested name and up to three of the
  closest monitor names — how an administrator spots a typo, or a service nobody
  wrote an alias for;
- an alias whose ids are absent from `/metrics` logs those ids.

Monitor names appear in those log lines on purpose. They never reach the sentence
returned to the model, or the user.

### What is deliberately not configurable

| Absent | Why |
| --- | --- |
| A cache or refresh interval | There is none, by design — see *Design decisions* |
| A general on/off switch | An empty URL already disables the connector, and Cheshire Cat can deactivate the plugin. A third switch could disagree with both |

## Testing

```bash
python run-tests.py --unit          # pure logic, local interpreter
python run-tests.py --integration   # adapter, inside the Cheshire Cat container
python run-tests.py                 # both
python run-tests.py --detailed      # ...listing every test name
```

The unit tier covers parsing, aliases, resolution, every outcome and the
packaging file list. The integration tier covers the settings, the validators,
the tool registration and the adapter's behaviour against a fake Cat and a
mocked HTTP call — including the case that matters most, an unreachable instance
yielding `unknown` with a sentence that claims neither that the service is up nor
that it is down. The runner prints how to start the container when it is not
running.

Neither tier contacts a live Uptime Kuma instance.

## Checking that a question actually invoked the tool

The chatbot's answer alone is not proof. Filter the core logs — do not share
their raw output, which may contain session tokens:

```powershell
docker compose logs --since 5m cheshire-cat-core 2>&1 |
  Select-String -Pattern '"action": "service_status"|intermediate_steps=.*service_status'
```

An `action` followed by an `intermediate_steps` entry such as
`(('service_status', 'Portale demo'), ...)` confirms that the question selected
the tool, passed it the name, and used the result.

The plugin's own success line adds the outcome and how the name was resolved:

```text
[uptime_kuma_connector] monitoring endpoint request succeeded (outcome: known, resolution: alias).
```

`resolution: alias` means the settings map was used, `name` means automatic name
matching, and `none` means nothing matched. Every line the plugin logs starts
with `[uptime_kuma_connector]`.

## Limitations

- **`pending` and `maintenance` are implemented but unobserved.** The `/metrics`
  fixtures the parser is tested against were captured from a real instance, and
  they contain `up` and `down`. `pending` appeared only transiently, with no
  complete line to capture, and `maintenance` could not be produced on that
  instance at all. Both are handled per Uptime Kuma's documented format, which
  is a weaker guarantee than a captured response.
- **`/metrics` carries no timestamp.** The Prometheus format asserts by
  convention that a value is current, so the plugin cannot tell whether Uptime
  Kuma measured it a second or an hour ago. A paused monitor was confirmed to
  disappear from `/metrics` rather than to keep reporting a stale state.
- **Automatic name matching imposes an operational prerequisite**: monitor names
  have to contain the term users actually say. Where they do not, that is what
  the alias map is for.

## Out of scope

- **Any write towards Uptime Kuma**, a heartbeat included: this is a read-only
  consumer, and the credentials to do more are deliberately absent.
- Configuring or modifying Uptime Kuma, or authenticating to its dashboard over
  Socket.IO.
- Receiving webhooks. A webhook keeps a cached state fresh, and there is no
  cached state here.
- Opening tickets, sending email, operational escalation.

## License

[GNU General Public License v3.0](LICENSE).
