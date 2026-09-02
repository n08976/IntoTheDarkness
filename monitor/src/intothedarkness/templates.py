"""Starter files written by ``itd init``."""

EXAMPLE_TARGETS = '''\
# IntoTheDarkness targets.
#
# Each entry is one thing to watch. Test selectors without touching the
# database:  itd targets test <name>
#
# scraper: css   repeating records picked out with CSS selectors
# scraper: page  one page (or region) watched as a whole for any change
# scraper: json  records read from a JSON endpoint

targets:
  - name: hn-front-page
    url: https://news.ycombinator.com/
    scraper: css
    interval_minutes: 30
    selectors:
      item: "tr.athing"
      title: "span.titleline > a"
      link: "span.titleline > a"
      attrs:
        rank: "span.rank"
    watch: [new]
    severity: info
    channels: [console]
    tags: [news]

  - name: example-page-watch
    url: https://example.com/
    scraper: page
    interval_minutes: 360
    # Narrow the watch to one region; omit to watch the whole body.
    selectors:
      text: "div"
    watch: [changed]
    severity: low
    channels: [console]
    tags: [uptime]
    enabled: false

  # --- Stage 1: leak-site victim monitoring -------------------------------
  # Sends one full BASELINE report on the first run, then deltas only.
  # Paste the onion address you want to watch and adjust the two selectors;
  # `itd targets test <name>` shows exactly what comes out.
  - name: dls-example
    url: http://REPLACE-WITH-56-CHAR-V3-ADDRESS.onion/
    scraper: dls
    network: tor            # auto would infer this; explicit fails loudly
    interval_minutes: 360
    selectors:
      item: ".victim-card"  # each victim entry
      title: "h3"           # the organisation name
      text: ".description"  # optional context, scanned for indicators
    watch: [new, removed]
    report_baseline: true
    content_mode: hash      # "store" to keep page bodies on disk
    severity: high
    channels: [email]
    tags: [dls, ransomware]
    enabled: false

  - name: example-json-feed
    url: https://api.example.com/v1/incidents
    scraper: json
    interval_minutes: 15
    json_path: "data.incidents"
    json_fields:
      key: "id"
      title: "name"
      url: "html_url"
      text: "body"
      status: "attributes.status"
    include: "outage|degraded"
    watch: [new, changed]
    severity: high
    channels: [email]
    tags: [status]
    enabled: false
'''

EXAMPLE_RULES = '''\
# Rules run over every finding, in order.
#
# Conditions (all must hold): targets (glob), kinds, tags, match, not_match.
# Effects: action (alert|ignore), severity (raises only), channels, stop.

rules:
  - name: drop-noise
    action: ignore
    match: "cookie (policy|banner)|newsletter signup"

  - name: escalate-breach-language
    match: "breach|leaked|credential dump|ransomware"
    severity: critical
    channels: [email]
    stop: true

  - name: scrape-failures-are-worth-knowing
    kinds: [error]
    severity: medium
    channels: [console]

  - name: status-tag-goes-to-email
    tags: [status]
    channels: [email]

  # --- Sector filtering ------------------------------------------------------
  # "Only tell me about healthcare." Order matters: the keep-rule must set
  # `stop: true`, or the catch-all ignore below swallows it too. `itd targets
  # validate` warns if you get this wrong.
  #
  # - name: healthcare-only
  #   targets: ["dls-*"]
  #   sectors: [healthcare]
  #   severity: critical
  #   channels: [email]
  #   stop: true            # <- required, or the next rule drops these
  # - name: ignore-other-sectors
  #   targets: ["dls-*"]
  #   action: ignore
'''

def _sectors_template() -> str:
    """Build the sectors file from the code's own defaults.

    Previously this was a hand-written subset of ``DEFAULT_SECTORS``. The two
    drifted, and since the file *overrides* the built-ins, an install silently
    ran on the older, shorter keyword list. One source of truth avoids that.
    """
    import yaml

    from .enrich.sector import DEFAULT_SECTORS

    header = (
        "# Industry sectors, matched case-insensitively against the organisation\n"
        "# name. Longer keywords win ties; anything unmatched is reported as\n"
        '# "unknown" rather than guessed at.\n'
        "#\n"
        "# This file OVERRIDES the built-in list — delete it to track the\n"
        "# built-ins as they change, or edit it to fit the sectors you report on.\n"
        "# Try a name against the current list with:\n"
        '#   itd sector classify "Some Company Ltd"\n'
        "#\n"
        "# Surrounding text is deliberately NOT consulted: on a leak site it\n"
        "# describes the stolen data, not the victim's industry. Set\n"
        "# ITD_SECTOR_USE_CONTEXT=true to change that.\n\n"
    )
    body = yaml.safe_dump(
        {"sectors": {k: list(v) for k, v in DEFAULT_SECTORS.items()}},
        sort_keys=False,
        default_flow_style=False,
        width=88,
    )
    return header + body


EXAMPLE_SECTORS = _sectors_template()

ENV_EXAMPLE = '''\
# Copy to .env and fill in. Every setting is also a plain environment variable.

# --- storage -----------------------------------------------------------------
# ITD_DATA_DIR=./data
# ITD_DB_URL=sqlite:///./data/intothedarkness.db
# ITD_TARGETS_FILE=./config/targets.yaml
# ITD_RULES_FILE=./config/rules.yaml

# --- fetching ----------------------------------------------------------------
ITD_USER_AGENT=IntoTheDarkness/0.1 (+monitoring bot)
ITD_REQUEST_TIMEOUT=20
ITD_PER_HOST_DELAY=1.0
ITD_RESPECT_ROBOTS=true

# --- email -------------------------------------------------------------------
ITD_SMTP_HOST=smtp.example.com
ITD_SMTP_PORT=587
ITD_SMTP_USER=
ITD_SMTP_PASSWORD=
ITD_SMTP_STARTTLS=true
ITD_EMAIL_FROM=itd@example.com
# JSON list, because it is a list-typed setting:
ITD_EMAIL_TO=["you@example.com"]

# --- webhook -----------------------------------------------------------------
# ITD_WEBHOOK_URL=https://hooks.slack.com/services/...

# --- tor ---------------------------------------------------------------------
# Bring your own tor daemon (system service or container). httpx passes the
# hostname to the proxy, so .onion resolves inside Tor and there is no DNS leak.
ITD_TOR_ENABLED=true
ITD_TOR_SOCKS_URL=socks5://127.0.0.1:9050
ITD_TOR_CONTROL_PORT=9051
# ITD_TOR_CONTROL_PASSWORD=
ITD_TOR_TIMEOUT=90
ITD_TOR_MAX_RETRIES=2
ITD_TOR_DELAY=2.0
ITD_ONION_VERIFY_TLS=false
ITD_TOR_ROTATE_ON_FAILURE=true
# Mask onion addresses in logs so shipped logs do not publish your target list.
ITD_REDACT_ONION_IN_LOGS=true

# --- content retention -------------------------------------------------------
# "hash" records only a digest; "store" writes bodies to data/snapshots.
# Targets can override this; `itd snapshot fetch <target> --store` is on demand.
ITD_CONTENT_MODE=hash
ITD_MAX_ITEM_TEXT=20000
ITD_SNAPSHOT_MAX_BYTES=10000000

# --- alerting ----------------------------------------------------------------
# Minutes before the same finding may alert again.
ITD_ALERT_COOLDOWN_MINUTES=360
ITD_LOG_LEVEL=INFO
'''


EXAMPLE_ENGINES = '''\
# Onion search engines used by `itd discover search`.
#
# These are onion addresses and they die like any other — this file is
# configuration, not a promise that any of them is up. Check with:
#   itd discover engines --check
#
# result_selector restricts extraction to links inside matching containers.
# When it is set and matches nothing, that engine yields NO candidates rather
# than falling back to scraping every link on the page: an unrecognised layout
# should produce silence, not a page of navigation junk. Leave it empty only
# for engines whose result markup you have actually checked.
#
# Engine list adapted from OpenTor (github.com/vichhka-git/OpenTor, MIT).

engines:
  - name: ahmia
    url: "http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion/search/?q={query}"
    result_selector: "li.result a, .result a"
  - name: onionland
    url: "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q={query}"
    result_selector: ".result-block a, .result a"
  - name: amnesia
    url: "http://amnesia7u5odx5xbwtpnqk3edybgud5bmiagu75bnqx2crntw5kry7ad.onion/search?query={query}"
    result_selector: ".result a, .search-result a"
  - name: torland
    url: "http://torlbmqwtudkorme6prgfpmsnile7ug2zm4u3ejpcncxuhpu4k2j4kyd.onion/index.php?a=search&q={query}"
    result_selector: ".result a"
  - name: excavator
    url: "http://2fd6cemt4gmccflhm6imvdfvli3nf7zn6rfrwpsy7uhxrgbypvwf5fad.onion/search?query={query}"
    result_selector: ".result a"
  - name: onionway
    url: "http://oniwayzz74cv2puhsgx4dpjwieww4wdphsydqvf5q7eyz4myjvyw26ad.onion/search.php?s={query}"
    result_selector: ".result a"
  - name: tor66
    url: "http://tor66sewebgixwhcqfnp5inzp5x5uohhdy3kvtnyfxc2e5mxiuh34iid.onion/search?q={query}"
    result_selector:
  - name: oss
    url: "http://3fzh7yuupdfyjhwt3ugzqqof6ulbcl27ecev33knxe3u7goi3vfn2qqd.onion/oss/index.php?search={query}"
    result_selector: ".result a"
  - name: torgol
    url: "http://torgolnpeouim56dykfob6jh5r2ps2j73enc42s2um4ufob3ny4fcdyd.onion/?q={query}"
    result_selector: ".result a"
  - name: deepsearches
    url: "http://searchgf7gdtauh7bhnbyed4ivxqmuoat3nm6zfrg3ymkq6mtnpye3ad.onion/search?q={query}"
    result_selector: ".result a"
  - name: ddg-onion
    url: "https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/?q={query}&ia=web"
    result_selector:
  - name: ahmia-clearnet
    url: "https://ahmia.fi/search/?q={query}"
    result_selector: "li.result a, .result a"
    # clearnet host; routed over Tor unless you opt out

# Extra terms for the content filter, on top of the built-in list. Matching
# results are withheld from output and counted, never printed.
# block_terms:
#   - some-local-term
'''
