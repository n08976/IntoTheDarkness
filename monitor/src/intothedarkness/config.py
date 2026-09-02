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
