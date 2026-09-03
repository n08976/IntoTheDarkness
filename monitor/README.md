# IntoTheDarkness

Scrape sites, notice what changed, decide whether it matters, tell someone, and
keep a case file about it.

A small Python toolkit with four moving parts:

| Part | What it does |
| --- | --- |
| **Scrapers** | Turn a URL into a list of items (CSS, whole-page, or JSON) |
| **Diffing** | Compare against stored state → `new` / `changed` / `removed` findings |
| **Rules** | Filter, escalate, and route findings to channels |
| **Notifiers** | Email (SMTP), webhook, console |
| **Investigations** | Case files: findings, notes, hashed evidence, exported reports |
| **Tor** | `.onion` targets over SOCKS5, with a hard clearnet/Tor boundary |

Targets and rules are YAML. Nothing is compiled in.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
itd init
```

`itd init` creates `data/`, the SQLite database, `config/targets.yaml`,
`config/rules.yaml`, and `.env.example`.

## Quick start

```bash
itd targets list                  # what's configured, and when it last ran
itd targets test hn-front-page    # fetch once, print extracted items, touch nothing
itd targets validate              # parse config, check selectors/channels
itd run --dry-run                 # full pass, print to console, persist nothing
itd run                           # for real
itd watch --interval 300          # loop, reloading config each sweep
```

## Defining a target

```yaml
targets:
  - name: hn-front-page
    url: https://news.ycombinator.com/
    scraper: css
    interval_minutes: 30
    selectors:
      item: "tr.athing"           # the repeating container
      title: "span.titleline > a" # read within each container
      link: "span.titleline > a"
      attrs:
        rank: "span.rank"         # "selector@attribute" also works
    include: "security|breach"    # optional regex filter
    watch: [new, changed]
    severity: medium
    channels: [email]
    tags: [news]
```

Four scrapers ship in the box:

- **`css`** — repeating records. Needs `selectors.item`.
- **`page`** — one page or region watched as a whole. Every edit is a `changed`
  finding. Narrow it with `selectors.text` so a rotating ad doesn't wake you.
- **`json`** — records from an API. `json_path` locates the list, `json_fields`
  maps dotted paths onto `key`/`title`/`url`/`text` plus any extras you name.
- **`dls`** — leak-site victim listings. Keys on the *normalised company name*,
  not the URL, so a site that rotates onion addresses doesn't re-report everyone.
- **`embedded`** — records from JSON a JavaScript app left in its own HTML
  (Inertia `data-page`, Next.js `__NEXT_DATA__`, Nuxt `window.__NUXT__`).

### A splash page is not necessarily the whole site

One live leak site served a landing page advertising "263 Companies" that
contained none of them, and no embedded JSON either. Its own inline JavaScript
gave the answer:

```js
fetch("/archive.php?last")
```

`/archive.php` returns all 263 as plain HTML. Reading the page's scripts for the
endpoints they call is worth doing before concluding a site needs a browser —
`grep` for `fetch(`, `.php`, `/api/` in the served HTML.

### An empty page is not necessarily a browser problem

A single-page app serves an empty `<body>` and builds the page with JavaScript,
so an HTTP fetch appears to return nothing. But most such apps ship their
initial state as JSON *inside that same HTML* — the records are already in the
bytes you fetched.

One live leak site looked unscrapeable this way. Its victim list turned out to
be 42 entries in an Inertia `data-page` payload, extracted with no browser at
all:

```yaml
  - name: dls-everest
    scraper: embedded
    json_path: "props.categories"
    json_fields: { key: id, title: title, published: date }
```

This matters beyond convenience. A headless browser is a second network stack
that has to be routed through Tor correctly, with its own fingerprint and its
own leak surface. Check for embedded JSON before reaching for one — the
`embedded` scraper names the payloads it found when a path misses, and says
plainly when a page *genuinely* has no data in it.

Add your own by dropping a function in `scrapers/`:

```python
from intothedarkness.scrapers import register_function

@register_function("my-site")
def scrape(target, fetcher):
    resp = fetcher.get(target.url)
    return [Item(key=..., target=target.name, title=...)]
```

It becomes usable as `scraper: my-site`.

### Item identity

Diffing is only as good as the item key. Identity is taken from
`selectors.key` if set, else the link, else a hash of title and text — so a
listing that reshuffles its order doesn't read as churn. A site with unstable
URLs (session ids, tracking params) needs an explicit `key`.

The **first run of a new target is seeded silently**. You get the back
catalogue as stored state, not as a hundred alerts. `itd targets forget <name>`
resets a target to that state.

## Tor / `.onion` targets

**The tool carries its own Tor.** No system package, no Docker, no desktop
browser — those all make the tool less portable than the list it monitors.

```bash
itd tor install        # fetch + verify the Tor Expert Bundle into data/tor
itd tor up             # start it, streaming bootstrap progress
itd tor down
itd tor where          # which binary would be used, and is one running
```

`itd tor install` downloads the same standalone `tor` that ships inside Tor
Browser, from Tor Project's archive, and checks it against their published
SHA-256 — a mismatch aborts. It picks the right build for Linux, macOS or
Windows (x86_64/i686/aarch64), needs no root, and writes nothing outside
`data/`. `lyrebird` (obfs4) comes with it, so bridges work with no extra
install.

If bootstrap **stalls below 25%**, the network is throttling or blocking
connections to Tor relays:

```bash
itd tor up --bridges meek        # try this first
itd tor up --bridges snowflake
itd tor up --bridges obfs4
```

`--bridges meek` tunnels Tor inside ordinary HTTPS to a CDN. On a network where
normal web traffic is fine but sustained relay TLS is starved, it is usually the
only transport that gets through — verified: direct Tor stalled at 20–30%
indefinitely on the network this was built on, and meek bootstrapped to 100% in
under two minutes. Bridge lines come from the Expert Bundle itself, so there is
nothing to fetch and no captcha.

`itd tor up` runs it as a managed child process on free ports, with its own
`torrc` and data directory, and streams bootstrap:

```
    0%  Starting
    5%  Connecting to a relay
   20%  Establishing an encrypted directory connection
  100%  Done
✓ bootstrapped (socks 41337, control 41339)
```

Other options still work if you prefer them — Tor Browser's proxy on 9150, a
system daemon on 9050, or containers. See [`deploy/README.md`](deploy/README.md).

```bash
itd tor status                    # which of the three states are we in?
itd tor status --identity         # ...and what exit IP does it present?
itd tor check-address <addr>      # validate shape before wasting a 90s timeout
```

`itd tor status` separates three states that a plain port check conflates:

| | meaning |
|---|---|
| no listener | Tor isn't running, or is on another port (it checks 9050 **and** 9150) |
| listening, not routing | running but **still bootstrapping** — wait, don't reconfigure |
| usable | a circuit exists; onion fetches will work |

The middle state is the time-waster: the port is open, so a TCP connect says
"fine", and then every fetch fails. `itd` does a full SOCKS5 CONNECT instead,
so it can tell the difference. Bootstrap stalling at 10–25% means your network
blocks or throttles Tor relay connections — use bridges; `deploy/torrc` has the
lines ready to uncomment.

Any `.onion` URL routes over Tor automatically. Set `network: tor` explicitly to
make a misrouted target fail loudly instead of leaking a lookup to clearnet DNS,
or `network: tor` on a clearnet URL to fetch it through Tor anyway.

**No DNS leak by construction.** httpx hands the hostname to the SOCKS proxy
rather than resolving it locally (verified: httpcore sends SOCKS5 address type
`0x03`, `DOMAIN_NAME`). So `socks5://` and `socks5h://` behave identically here,
unlike in `requests` or `curl`, and there is no local resolution to leak.

Tor gets its own network profile, separate from clearnet:

| | clearnet | tor |
| --- | --- | --- |
| timeout | 20s | 90s |
| retries | 3 | 2 (retrying a bad circuit is mostly waste) |
| robots.txt | respected | ignored — noise on hidden services |
| TLS verify | on | off — v3 onions are self-authenticating |
| pacing | per host | per host **and** across the whole circuit |

**Redirects never cross the boundary.** A `.onion` that 302s to clearnet (or the
reverse) is refused, not followed — that hop would change your threat model
silently. Onion addresses are masked in logs and errors
(`ITD_REDACT_ONION_IN_LOGS`) so shipped logs don't publish your target list.

Circuit rotation via Tor's ControlPort needs the optional extra:

```bash
pip install -e ".[tor]"     # adds stem
```

Without it everything still works; failed fetches just retry instead of
rotating. Rotation only fires on circuit-shaped errors (connect/read timeouts,
proxy errors), never on a 404, and is rate-limited on both sides — Tor throttles
`NEWNYM`, and `tor_min_rotate_interval` guards it again.

## Content retention

Default is **hash-only**: a digest and metadata are recorded, never the body.
Monitoring hidden services can pull in material you don't want on disk, so
keeping bodies is opt-in.

```bash
itd snapshot fetch <target>            # hash only
itd snapshot fetch <target> --store    # keep the body too
itd snapshot list
itd snapshot show <target> <sha256> --out page.html
itd snapshot purge --target <target>   # bodies go, hashes stay
```

Or set `content_mode: store` on a target. Bodies are gzipped and named by
digest, so identical content is never stored twice. `max_item_text` bounds what
goes into the database regardless.

## The curated list is the source of truth

[`bookmarks.json`][repo] — the hand-maintained list of onion sites, browsed
directly in Tor Browser — is what decides *what is worth watching*. This tool
reads it, monitors it, and proposes additions back to it. It never edits the
list on its own.

[repo]: https://github.com/n08976/IntoTheDarkness

```bash
export ITD_BOOKMARKS_FILE=/path/to/IntoTheDarkness/bookmarks.json

itd bookmarks status                     # what is in the list, and what cannot resolve
itd bookmarks check --onion-only         # which entries are alive, over Tor
itd import bookmarks -o config/targets.d/bm.yaml    # project it into monitoring targets
itd bookmarks propose --from-findings    # onions discovered while scraping
itd bookmarks add "Group Blog" http://<v3>.onion/   # add one by hand
```

**Writes preserve the file's authored style byte-for-byte.** The list keeps each
link on one line (`{ "title": ..., "url": ... }`); a naive `json.dumps` would
reflow all 75 entries and turn a one-link addition into a 400-line diff. Adding
a link changes **three lines**, and `generate.py` runs against the result
unmodified. A test asserts the round-trip is byte-exact.

Additions are deduplicated on *host*, so the same site at a different scheme or
path is never added twice. v2 addresses and malformed hostnames are rejected
with a reason rather than silently dropped.

### Checking what is still alive

The list's own note says addresses rotate frequently and to verify before
relying on any one of them. That is a command:

```bash
itd bookmarks check --onion-only --out health.json
  ✗ Some Group              All connection attempts failed
  ✓ Another Group           Leaked Data | index
  · SafePay (TON)           tonsite: scheme is not fetchable over HTTP

  62 checked · 41 alive · 19 dead · 0 invalid · 2 skipped
```

Results go to the console and optionally to JSON. **`bookmarks.json` is never
modified by a check** — dead entries are a judgement call, not a fact to
overwrite someone's file with. Deliberate non-HTTP entries (`about:manual`,
`tonsite://`) are skipped, not reported dead.

### Finding new sites

Discovery queries onion search indexes for sites **not already in the list**:

```bash
itd discover engines            # what is configured
itd discover engines --check    # which of them actually answer
itd discover search "ransomware leak site"
itd discover search "acme corp" --apply     # add candidates to bookmarks.json
```

Engines live in `config/engines.yaml`, not in code — they are onion addresses
and they die like everything else. The catalogue is adapted from
[OpenTor][opentor] (MIT).

[opentor]: https://github.com/vichhka-git/OpenTor

Output feeds a human-reviewed git commit, so it is tuned for **precision over
recall**:

- Onions are read from link `href`s only, never from body text
- An engine whose `result_selector` matches nothing yields **nothing** — an
  unrecognised layout produces silence, not a page of navigation links
- v3 addresses only; engine self-links, nav titles and stubs are dropped
- Ranking is by **how many distinct engines returned the same address**, because
  search indexes are spammed and one hit is not evidence

The corroboration bar adapts: with three or more engines responding, two must
agree; with fewer, one will do. Override with `--min-engines`.

Queries are **sequential**, not parallel — every request shares one Tor circuit,
so concurrency just queues requests behind each other and makes timeouts harder
to attribute.

Two things worth knowing before you run it:

- **The query goes to each engine's operator.** It is never written to logs or
  the database (`ITD_LOG_SEARCH_QUERIES=false` by default).
- **Clearnet indexes are fetched over Tor anyway**, so a discovery run does not
  put your query on the local network's DNS. `--allow-clearnet` opts out.

Results matching a content filter are **withheld and counted** — the count is
always reported, so nothing is silently lost, and there is no flag that prints
them back out. Add local terms under `block_terms` in `config/engines.yaml`.

### Feeding discoveries back

Scraped pages leak links to other hidden services. Those are extracted as
indicators, and `propose` collapses them into candidate entries — deduplicated
against the list, ranked by how many distinct sources saw them:

```bash
itd bookmarks propose --from-findings --since-hours 168
itd bookmarks propose --from-findings --apply      # writes; review the diff
```

An address seen on two independent leak sites outranks one seen once. Nothing
is written without `--apply`.

## Getting a bulk target list

To seed the curated list in bulk, or cross-check it, import from a
ransomwatch-format `groups.json`:

```bash
itd import ransomwatch --show-skipped -o config/targets.d/dls.yaml
```

The default source is [cyberiskvision/dls-monitor][dls-monitor], a live fork of
[ransomwatch][ransomwatch]. Check freshness before trusting any such list —
upstream ransomwatch stopped updating in June 2025, while the fork is current.
Any file in that format works, local or remote.

[dls-monitor]: https://github.com/cyberiskvision/dls-monitor
[ransomwatch]: https://github.com/joshhighet/ransomwatch

What the import does with a recent snapshot:

```
✓ 15 target(s) from 182 group(s) / 367 host(s); 167 skipped
  162 no reachable v3 mirror
    3 needs JavaScript rendering   blackout, lockbit3, redransomware
    2 captcha-gated                cloak, clop
```

Imported targets arrive **disabled**, set to `network: tor`, `content_mode:
hash`, and whole-page change detection — an import never starts hitting hidden
services on its own. Extra mirrors go into `notes` rather than becoming separate
targets, since they serve the same content and would double-report.

That list carries *addresses*, not selectors — ransomwatch uses hand-written
per-site Python parsers. So each site needs its own selectors, which is what the
next section is for.

### Suggesting selectors

```bash
itd targets suggest dls-akira          # fetch and analyse
itd targets suggest --file saved.html  # or work offline
```

It finds the repeated structure a listing is built from and proposes an
`item`/`title` pair with samples:

```
1. 14 entries  (score 9.03)
     selectors:
       item: "div.post-card"
       title: "h3.company"
     samples:
       · Northwind Traders
       · St Mary Regional Hospital
```

Paste that in, switch the target to `scraper: dls`, enable it. **Read the
samples, not the ranking** — it is reliable on the card, table and list layouts
leak sites use, and weakest on deeply nested table markup where every row is a
`tr` and structure carries no meaning.

### Skipping history

A site you have never scraped will baseline its entire back catalogue. If the
source ships a `posts.json`, seed the known victims first so the baseline covers
only what is genuinely new:

```bash
itd import ransomwatch-posts dls-ransomhouse posts.json -g ransomhouse --dry-run
itd import ransomwatch-posts dls-ransomhouse posts.json -g ransomhouse
```

Seeded keys are computed exactly as the `dls` scraper computes them, so a victim
already in the history is not reported again when it is first scraped.

## Stage 1: leak-site monitoring

Watch a DLS for victim names, get one full report, then only what changed.

```yaml
targets:
  - name: dls-example
    url: http://<56-char-v3-address>.onion/
    scraper: dls
    network: tor
    interval_minutes: 360
    selectors:
      item: ".victim-card"    # each victim entry
      title: "h3"             # the organisation name
      text: ".description"    # optional context, scanned for indicators
    watch: [new, removed]
    report_baseline: true     # full list once, deltas thereafter
    content_mode: hash
    severity: high
    channels: [email]
```

The first run emails a **baseline** — every victim currently listed, grouped by
sector, subject-lined as a baseline rather than an alert so nobody reads 200
entries as 200 events. Every run after that reports only new and delisted
victims.

Victim identity is the normalised company name, so `Acme Steel Ltd`,
`ACME STEEL LIMITED` and `Acme Steel Corp.` are one victim. Site furniture
(`READ MORE`, sizes, dates, percentages) is stripped before matching. This is
what stops a site reshuffling or moving address from looking like a hundred new
breaches.

Indicators found in each entry — emails, BTC/ETH/XMR addresses, PGP blocks,
other `.onion` links — are attached to the item automatically.

### Sector filtering

Every victim carries a sector **and where that label came from**, because
routing on a label is only safe if you can tell a stated fact from a guess:

| `sector_source` | meaning |
| --- | --- |
| `target` | stated in the target config |
| `upstream` | the source published its own industry label |
| `propagated` | an authoritative index classifies this same victim |
| `name` | keyword match on the organisation name |
| `domain` | keyword match on the victim's domain |
| `none` | no evidence — reported as `unknown`, never guessed |

Stronger evidence always wins; a guess never displaces a stated fact. **Scrapers
do not classify** — the pipeline does, so a guess made while scraping cannot be
laundered into something that looks like the source's own claim.

#### The authoritative index

Keyword-matching a company name leaves most victims unlabelled — measured at
**61% unknown** across one leak site's 263 entries. "Easterseals", "Community
Care Alliance" and "Florida Lung" are plainly healthcare to a human and
invisible to a keyword list.

```bash
itd sector index --refresh                    # healthcare by default
itd sector index --refresh -s healthcare -s finance
itd sector index                              # what is cached
```

This downloads victims already classified by sector from ransomware.live. On
that same leak site it took healthcare from **22 to 44** — double the recall,
every addition backed by a stated classification rather than a better guess.

The API allows about one request a minute, so the index is cached on disk and
refreshed deliberately; a monitoring run never refetches it. Only distinctive
names are matched — "Summit", "ACME" and "N/A" are refused, because a wrong
sector silently misroutes an alert.

#### Routing one sector

```yaml
default_action: ignore      # keep only what a rule matches

rules:
  - name: healthcare-confirmed
    sectors: [healthcare]
    sector_sources: [target, upstream, propagated]
    severity: critical
    channels: [email]

  - name: healthcare-inferred
    sectors: [healthcare]
    sector_sources: [name, domain]
    severity: high
    channels: [email]
```

`default_action: ignore` is the direct way to say "only these" — no catch-all
ignore rule and no `stop: true`. Splitting confirmed from inferred means a
keyword guess arrives at a different severity than a stated fact, rather than
both looking equally certain.

Measured across all five sources: 529 victims scraped, **68 healthcare**
(57 propagated, 7 upstream, 4 name).

#### The keyword vocabulary

Names are matched against `config/sectors.yaml`
(keyword matching; unmatched names are `unknown`, never guessed).

```bash
itd sector list
itd sector classify "St Mary Regional Hospital"   # -> healthcare
itd findings --sector healthcare --kind new
```

To alert on one sector only, **the keep-rule must set `stop: true`**:

```yaml
rules:
  - name: healthcare-only
    targets: ["dls-*"]
    sectors: [healthcare]
    severity: critical
    channels: [email]
    stop: true              # without this, the next rule drops these too
  - name: ignore-other-sectors
    targets: ["dls-*"]
    action: ignore
```

`itd targets validate` warns if you get that ordering wrong.

## Rules

Rules run in order over every finding. All stated conditions must hold.

```yaml
rules:
  - name: drop-noise
    action: ignore
    match: "cookie policy|newsletter signup"

  - name: escalate-breach-language
    match: "breach|leaked|credential dump"
    severity: critical      # raises only, never lowers
    channels: [email]
    stop: true              # skip remaining rules
```

Conditions: `targets` (glob), `kinds`, `tags`, `sectors`, `match`, `not_match`.
Effects: `action` (`alert`/`ignore`), `severity`, `channels`, `stop`.

## Seeing a report before wiring up email

```bash
itd run --dry-run       # print it, persist nothing, send nothing
itd run --no-notify     # detect and record, send nothing
itd run --preview       # write the real HTML report to data/previews/
itd findings            # query what was recorded
```

`--dry-run` already *is* the plain-text email — the console channel and the
email body use the same renderer. What it cannot show you is the HTML part,
which is what actually renders in an inbox. `--preview` writes that to a file,
subject line included, and prints the path.

`--preview` overrides channels at dispatch, after rules have run, so a rule that
adds `email` cannot quietly send something you asked only to preview.

## Alerting

Channels: `console`, `preview`, `resend`, `email`, `webhook`.

**Resend is the recommended path** — an HTTPS API call, so no STARTTLS
negotiation and none of the certificate-name traps that make shared-hosting SMTP
fragile:

```bash
ITD_RESEND_API_KEY=re_...
ITD_EMAIL_FROM='IntoTheDarkness <itd@yourdomain.com>'
ITD_EMAIL_TO='["you@example.com"]'    # JSON list
```

SMTP works too:

```bash
ITD_SMTP_HOST=premium215.web-hosting.com   # the SERVER's hostname
ITD_SMTP_PORT=587
ITD_SMTP_USER=noreply@yourdomain.com
ITD_SMTP_PASSWORD=...
```

On shared cPanel hosting, point `ITD_SMTP_HOST` at the **server's own
hostname**, not your domain or `mail.<domain>` — the domain's certificate does
not cover the mail host, and verification fails with an error most clients
misreport. Sending is **refused outright** if the server does not offer
STARTTLS, rather than downgrading to plaintext: victim names should not cross an
unencrypted link.

Then `itd notify-test --channel email` to prove it works before relying on it.

The same finding will not alert twice within `ITD_ALERT_COOLDOWN_MINUTES`
(default 6 hours). A `changed` finding folds content into its dedupe key, so a
page that keeps changing keeps alerting; a stable new item alerts once.

## Investigations

```bash
itd findings --since-hours 48
itd case open "Fake support portals" --tag phishing
itd case link-findings fake-support-portals 14 15 16
itd case note fake-support-portals "Same registrar as the March cluster."
itd case attach fake-support-portals ./screenshot.png
itd case show fake-support-portals
itd case export fake-support-portals --format md
```

Attached files are copied into `data/cases/<slug>/` and hashed with SHA-256, and
the exported report states those hashes — so it records what was collected and
that it hasn't changed since.

## Being a good citizen

`robots.txt` is respected by default and there is a one-second per-host delay
between requests. Both are configurable (`ITD_RESPECT_ROBOTS`,
`ITD_PER_HOST_DELAY`) and both exist for a reason. Scrape sites you're allowed
to scrape, at a rate that doesn't cost them anything.

## Layout

```
src/intothedarkness/
  cli.py            typer CLI (itd)
  config.py         settings from env/.env
  models.py         Target, Item, Finding, Severity
  tor.py            onion validation, log redaction, circuit control
  loader.py         YAML → Target/Rule/sectors, with useful errors
  pipeline.py       scrape → diff → enrich → rules → dedupe → notify → record
  scrapers/         fetch.py (network profiles, retries, throttle, robots)
                    html.py, json_api.py, dls.py, suggest.py
  enrich/           ioc.py (indicators), sector.py (industry labelling)
  importers/        ransomwatch.py (groups.json / posts.json)
  bookmarks/        store.py (style-preserving IO), health.py, discover.py
  discovery/        engines.py (catalogue), search.py (parsing), safety.py
  storage/          db.py, repository.py (diffing), snapshots.py (retention)
  alerting/         rules.py
  notify/           email.py, webhook.py, console.py, render.py
  investigations/   case.py
```

`tests/socks_stub.py` is a minimal in-process SOCKS5 server, so the whole Tor
path is tested without a running daemon — including that the hostname reaches
the proxy rather than being resolved locally.

## Development

```bash
pytest              # tests
ruff check src      # lint
mypy                # types
```
