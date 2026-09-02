# External projects and sources

Everything consulted while building this, including what was evaluated and
**not** used. Recorded so the reasoning survives, and so nobody re-evaluates the
same thing twice.

Verification dates are 2026-09-02. Onion addresses and project activity both
rot; re-check before relying on any of it.

---

## Used

### Tor Expert Bundle — Tor Project
<https://www.torproject.org/download/tor/> · archive: `archive.torproject.org/tor-package-archive/torbrowser/`
**BSD 3-Clause** · **Used: this is how the tool gets Tor.**

The same standalone `tor` that ships inside Tor Browser. `itd tor install`
fetches the bundle for the host platform, verifies it against Tor Project's
published `sha256sums-unsigned-build.txt`, and unpacks it under `data/tor/`.

- Verified 15.0.21 downloads and its checksum matches the published sums.
- Platforms available: `linux-{x86_64,i686}`, `macos-{x86_64,aarch64}`,
  `windows-{x86_64,i686}`, plus Android variants.
- Ships `lyrebird` (the obfs4 pluggable transport), so bridges work with no
  extra install — which matters on networks that throttle Tor relays.

Chosen over a system package or Docker because both make the tool less portable
than the list it monitors. See `../deploy/README.md`.

### cyberiskvision/dls-monitor
<https://github.com/cyberiskvision/dls-monitor> · **Unlicense**
**Used: `itd import ransomwatch` reads its `groups.json` / `posts.json`.**

A live fork of ransomwatch. Maintains the genuinely hard part — which groups
exist and which of their onion mirrors answer today.

**Verified live**: last scrape `2026-09-02 15:20`, posts through 2026-09-02.
182 groups / 367 locations, 179 enabled, **48 reachable**, 13,962 posts.

Of the 48 reachable hosts, 45 are plain HTML; 2 are captcha-gated (`cloak`,
`clop`) and 3 need JavaScript (`blackout`, `lockbit3`, `redransomware`).

It carries *addresses*, not selectors — upstream uses hand-written per-site
Python parsers. `itd targets suggest` exists to close that gap.

### vichhka-git/OpenTor
<https://github.com/vichhka-git/OpenTor> · **MIT**
**Partially used: the search-engine catalogue in `config/engines.yaml`.**

A Claude Code / OpenCode *skill*, not a library — there is nothing to depend on,
so this is a port of ideas.

**Taken**: the list of 12 onion search engines with their query-URL templates.
That is curated knowledge, and it is what `itd discover search` runs on.
Attribution is in `engines.py` and in the generated `config/engines.yaml`.

**Deliberately not taken**:

| Their approach | Ours, and why |
|---|---|
| Falls back to `soup.find_all("a")` when result selectors miss | An engine whose selector misses yields **nothing**. Output feeds a human-reviewed git commit, so an unrecognised layout must produce silence, not a page of nav links. |
| `ThreadPoolExecutor` across engines | Sequential. Every request shares one Tor circuit; concurrency just queues them and makes timeouts unattributable. |
| Content blacklist at the transport layer, silent and non-disableable | Filtered results are **withheld and counted**. Silent drops destroy the ability to say what you did and didn't see; a "show anyway" flag defeats the filter. The count is the honest middle. |
| BFS spidering, STIX/MISP export, analysis modes, 30-min cache | Out of scope — that is a collection product, not a change monitor. |

### joshhighet/ransomwatch
<https://github.com/joshhighet/ransomwatch> · **Unlicense**
**Referenced: the upstream and the file format. Data NOT used — it is stale.**

Last scrape `2025-06-17`; posts stop 2025-06-16. **14 months behind** the fork
above, despite being the better-known project. Anyone reaching for "the
well-known one" gets dead addresses and a 90-second timeout each.

The `groups.json` schema is shared, so the importer reads either.

### n08976/IntoTheDarkness
<https://github.com/n08976/IntoTheDarkness> · this repository
**Used: `bookmarks.json` is the source of truth for what to watch.**

75 links / 62 onion / 7 categories, all onion addresses well-formed v3.

Writes preserve the file's authored style byte-for-byte — each link on one line.
A naive `json.dumps(indent=2)` reflows all 75 entries and turns a one-link
addition into a ~400-line diff. Adding a link changes **three lines**, and
`generate.py` runs against the result unmodified.

Two entries are deliberately not HTTP-fetchable and are **skipped**, not
reported dead: `tonsite://safepay.ton` (needs a TON gateway) and `about:manual`
(a Tor Browser internal page).

### Python libraries
- **httpx** + **httpcore** + **socksio** — HTTP and SOCKS5. See the finding
  below; the SOCKS behaviour is why `.onion` resolution is safe here.
- **stem** (optional, `pip install ".[tor]"`) — Tor ControlPort, for `NEWNYM`
  circuit rotation. Everything works without it.
- **BeautifulSoup4** / **lxml** — HTML parsing.
- **pydantic** / **pydantic-settings** — models and configuration.
- **SQLAlchemy** — SQLite storage.
- **typer** / **rich** — CLI.
- **tenacity** — retries.
- **respx** — HTTP mocking in tests.

---

## Evaluated and not used

### dperson/torproxy (Docker image)
<https://hub.docker.com/r/dperson/torproxy>
Referenced in `deploy/docker-compose.yml` as one option, but **not the primary
answer** — requiring Docker is exactly the portability cost the Expert Bundle
avoids. Kept for people who already run containers.

### Tor Browser as the SOCKS provider (port 9150)
Works, and `itd tor status` detects it. **Rejected as the primary mechanism**:
it ties the tool to a desktop install and dies when the browser closes, which
rules out scheduled monitoring.

### System `tor` package (apt / brew)
Works. Not primary: needs root, differs per platform, and leaves state outside
the project.

### torpy (pure-Python Tor client)
Not adopted. A partial protocol implementation is a poor foundation when the
real client is a verified 30 MB download.

### OpenTor's meta-search — *initially* rejected, later adopted
Worth recording that this call **changed**. When the tool was purely a change
monitor, search was a different product. Once the workflow became "browse
manually, curate a list, hand new sites over", discovery became a stated need
and the call flipped. Implemented narrowly, as a proposal generator only.

---

## Findings worth keeping

### `socks5://` and `socks5h://` are identical in httpx
Most Tor documentation insists on `socks5h://` so DNS resolves through the
proxy. That is true for `requests` and `curl`. **It is not true for httpx.**

Read from `httpcore/_sync/socks_proxy.py`: it opens TCP only to the proxy, then
passes `self._remote_origin.host` as a string into the SOCKS5 handshake. There
is no `getaddrinfo` anywhere in that path. Confirmed by asserting `socksio`
encodes a `.onion` as address type `0x03` (`DOMAIN_NAME`).

So there is no local resolution to leak, and both schemes behave the same.
`tests/test_tor.py` asserts the proxy receives `0x03`, so this stays true if
httpx changes underneath.

### A bare TCP connect cannot tell "Tor is up" from "Tor works"
A bootstrapping tor accepts connections on its SOCKS port and then refuses to
route. Port checks report success; every fetch then fails. `itd tor status` does
a full SOCKS5 CONNECT and separates three states: no listener / listening but
not routing / usable.

This was a real bug in an earlier version of this tool.

### Bootstrap stalling below 25% means relay throttling
Observed directly while building this: TCP to relays connected fine, TLS
handshakes to relay ORPorts took **131 seconds** or timed out, and bootstrap
never passed 30%. Ordinary HTTPS was unaffected and the TLS chain showed no
interception, so this was traffic shaping, not filtering or a proxy MITM.

The fix is bridges (`ITD_TOR_BRIDGES`), and `lyrebird` ships in the Expert
Bundle for exactly this.

### Victim identity must not be the URL
Leak sites rotate onion addresses and reshuffle paths. Keying victims on the URL
re-reports everyone whenever a mirror changes. Identity is the normalised
company name, so `Acme Steel Ltd`, `ACME STEEL LIMITED` and `Acme Steel Corp.`
are one victim.

### Rule ordering has a trap
The obvious way to write "only healthcare" silently drops everything: a
catch-all `ignore` after a keep-rule swallows what the keep-rule matched unless
that rule sets `stop: true`. `itd targets validate` now names it.

---

## Consultations

Design reviewed with **GPT-5.2** via the PAL MCP server at three points:

1. **Tor transport design** — client-per-network pooling, robots.txt on hidden
   services, per-circuit throttling, timeout and retry posture, when circuit
   rotation is worth it, and the cross-network redirect boundary. Its sharpest
   catch was the redirect boundary; its DNS-leak warning turned out not to apply
   to httpx (see above), which is why that was verified in source rather than
   accepted.

2. **Whether to port OpenTor's meta-search** — agreed the scope call flips given
   the changed workflow, and that it must stay a proposal generator.

3. **Discovery design** — precision-first parsing, engine list as config rather
   than code, adaptive corroboration threshold, and the content-filter layering.
   It also caught that a clearnet index (`ahmia.fi`) would go direct under AUTO
   routing and leak the query to local DNS.
