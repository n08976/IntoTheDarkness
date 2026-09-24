"""Runtime settings, loaded from the environment and an optional .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ITD_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Storage
    data_dir: Path = PROJECT_ROOT / "data"
    db_url: str = ""
    targets_file: Path = PROJECT_ROOT / "config" / "targets.yaml"
    rules_file: Path = PROJECT_ROOT / "config" / "rules.yaml"
    sectors_file: Path = PROJECT_ROOT / "config" / "sectors.yaml"
    # The curated list, when checked out locally. Source of truth for targets.
    bookmarks_file: Path = PROJECT_ROOT / "bookmarks.json"
    engines_file: Path = PROJECT_ROOT / "config" / "engines.yaml"
    # Never record what was searched for: the query is the sensitive part,
    # and it has already been handed to the engine operator.
    log_search_queries: bool = False

    # HTTP behaviour. Be a polite citizen by default.
    user_agent: str = "IntoTheDarkness/0.1 (+monitoring bot)"
    request_timeout: float = 20.0
    max_retries: int = 3
    per_host_delay: float = 1.0
    respect_robots: bool = True
    verify_tls: bool = True

    # --- Tor -----------------------------------------------------------------
    # Bring your own tor: run it as a system service or a container. httpx sends
    # the hostname to the proxy for resolution, so .onion names resolve inside
    # Tor and socks5:// is equivalent to socks5h:// here.
    tor_enabled: bool = True
    tor_socks_url: str = "socks5://127.0.0.1:9050"
    tor_control_port: int = 9051
    tor_control_password: str = ""
    # Tor is slower and more variable than clearnet: wait longer, retry less.
    tor_timeout: float = 90.0
    tor_max_retries: int = 2
    # Delay between *any* two requests over Tor. The shared circuit is the
    # bottleneck, not the remote host, so this is separate from per_host_delay.
    tor_delay: float = 2.0
    # Onion v3 addresses are self-authenticating, and most services are plain
    # HTTP or use self-signed certs. Verifying by default just breaks fetches.
    onion_verify_tls: bool = False
    # Rotate the circuit (stem NEWNYM) after a network failure, then retry once.
    # Path to a tor binary. Empty means: use the one `itd tor install` fetched,
    # else whatever is on PATH.
    tor_binary: str = ""
    # Start and stop a bundled tor automatically around runs that need it.
    tor_autostart: bool = True
    tor_bootstrap_timeout: float = 180.0
    # obfs4 bridge lines, for networks that block or throttle Tor relays.
    tor_bridges: list[str] = Field(default_factory=list)
    tor_rotate_on_failure: bool = True
    tor_min_rotate_interval: float = 30.0
    # Keep onion hostnames out of logs and error strings.
    redact_onion_in_logs: bool = True

    # --- Content retention ---------------------------------------------------
    # "hash" records only a digest; "store" writes the body to data/snapshots.
    # Targets may override this. Monitoring hidden services can pull in material
    # you would rather not have on disk, so hashing is the default.
    # Let surrounding text influence sector labelling. Off by default: on leak
    # sites that text describes the stolen data, not the victim's industry.
    sector_use_context: bool = False
    content_mode: str = "hash"
    max_item_text: int = 20_000
    snapshot_max_bytes: int = 10_000_000

    # Email / SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    email_from: str = ""
    email_to: list[str] = Field(default_factory=list)

    # Resend HTTP API — the house pattern in this operator's other projects,
    # and less fragile than shared-hosting SMTP.
    resend_api_key: str = ""

    # How far back the running list in an emailed report reaches. The report
    # leads with what is new since the last one; this is the tail below it.
    digest_days: int = 60
    # Recipients (addresses or bare domains) whose copy of a report gets its
    # URLs rewritten as hxxp://host[.]tld. One institutional gateway drops any
    # report carrying raw .onion links and passes the same report de-fanged,
    # while a reader doing an investigation on another address needs the real
    # links. Each recipient gets the form its gateway accepts. "*" means all.
    defang_recipients: list[str] = Field(default_factory=list)

    # Every sector is reported; these lead the "new" section and are the only
    # ones carried in the running list (with the rest counted), because at
    # 30-180 victims a day across all sectors a full 60-day list is not an
    # email anyone reads. Empty digest_sectors means carry everything.
    priority_sectors: list[str] = Field(default_factory=lambda: ["healthcare"])
    digest_sectors: list[str] = Field(default_factory=lambda: ["healthcare"])

    # Priority watchlist: vendor names whose appearance anywhere is urgent.
    # The list is edited in the git repository and pushed; this box has no
    # checkout of it, so the published file is fetched each run into
    # watchlist_file, which also serves as the cache when the fetch fails.
    watchlist_file: Path = PROJECT_ROOT / "watchlist" / "vendors.txt"
    watchlist_url: str = (
        "https://raw.githubusercontent.com/n08976/IntoTheDarkness/main/monitor/watchlist/vendors.txt"
    )
    watchlist_channels: list[str] = Field(default_factory=lambda: ["resend"])
    # A vendor match re-alerts no sooner than this; the hourly watchlist
    # sweeps would otherwise repeat the same alert until the regular sweep
    # persists the finding.
    watchlist_cooldown_minutes: int = 1440
    # How often the wrapper runs a watchlist-only sweep between the scheduled
    # reports. Read by deploy/monitor-run.sh; cron fires every 15 minutes.
    watchlist_interval_minutes: int = 60
    # Vendors kept out of headline matching (still matched on leak sites and
    # in SEC filings, where a match is unambiguous): the biggest platforms
    # are also simply news, and "Microsoft" beside "stolen" reads as an
    # incident whether Microsoft is the victim or the one shutting it down.
    watchlist_headline_exclude: list[str] = Field(
        default_factory=lambda: [
            "Microsoft", "Google", "Cisco Systems", "Cisco", "Adobe Inc", "Adobe",
        ]
    )
    # Targets carrying any of these tags are news: their titles are headlines,
    # so a vendor counts when it is mentioned alongside an incident word,
    # rather than when the title *is* the vendor as on a leak site.
    watchlist_headline_tags: list[str] = Field(default_factory=lambda: ["news"])

    # SEC EDGAR: 8-K cyber-incident filings by watchlist vendors. EDGAR
    # requires a User-Agent naming a contact ("Name email@example.com").
    sec_user_agent: str = ""
    sec_window_days: int = 90
    sec_refresh_minutes: int = 240

    # Publish the same report as a web page: written into a git checkout and
    # pushed, so the host deploys it. Links are de-fanged on the page by
    # default -- it is public, and live .onion links on a public page invite
    # more than an inbox does.
    site_dir: Path | None = None
    site_push: bool = True
    site_defang: bool = True
    site_keep_reports: int = 400

    # Generic webhook sink
    webhook_url: str = ""

    # Alerting
    alert_cooldown_minutes: int = 360
    log_level: str = "INFO"

    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        return f"sqlite:///{self.data_dir / 'intothedarkness.db'}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "cases").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "snapshots").mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Process-wide settings singleton."""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
