"""Command line interface: ``itd``."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__
from .alerting import ordering_warnings
from .bookmarks import Bookmarks, Link, discover, health
from .bookmarks import save as save_bookmarks
from .config import get_settings
from .discovery import ContentFilter, extra_block_terms, load_engines
from .discovery import search as run_search
from .importers import ransomwatch
from .investigations import CaseManager
from .loader import ConfigError, load_rules, load_sectors, load_targets
from .models import Finding, FindingKind, Item, Severity
from .notify import Message, get_notifier, render_html, render_subject, render_text
from .notify import available as notify_channels
from .pipeline import Pipeline
from .scrapers import Fetcher
from .scrapers import available as scraper_names
from .scrapers.suggest import suggest as suggest_selectors
from .storage import Repository, SnapshotStore, get_db
from .tor import find_socks, is_onion, probe_socks, redact, validate_onion
from .tor_manager import (
    BRIDGE_PRESETS,
    ManagedTor,
    TorInstallError,
    TorLaunchError,
    bundled_bridges,
    resolve_binary,
)
from .tor_manager import install as install_tor
from .tor_manager import installed as tor_installed

console = Console()
err = Console(stderr=True)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="IntoTheDarkness — scrape, watch, alert, investigate.",
)
targets_app = typer.Typer(no_args_is_help=True, help="Inspect and test targets.")
case_app = typer.Typer(no_args_is_help=True, help="Investigations and case files.")
tor_app = typer.Typer(no_args_is_help=True, help="Tor transport status and checks.")
snap_app = typer.Typer(no_args_is_help=True, help="On-demand content snapshots.")
sector_app = typer.Typer(no_args_is_help=True, help="Industry sector labelling.")
app.add_typer(targets_app, name="targets")
app.add_typer(case_app, name="case")
app.add_typer(tor_app, name="tor")
app.add_typer(snap_app, name="snapshot")
import_app = typer.Typer(no_args_is_help=True, help="Import target lists from other tools.")
app.add_typer(sector_app, name="sector")
bm_app = typer.Typer(no_args_is_help=True, help="The curated bookmarks list.")
app.add_typer(import_app, name="import")
disco_app = typer.Typer(no_args_is_help=True, help="Find new sites via onion search engines.")
app.add_typer(bm_app, name="bookmarks")
app.add_typer(disco_app, name="discover")


def _setup_logging(verbose: bool) -> None:
    settings = get_settings()
    level = logging.DEBUG if verbose else getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)-7s %(name)s: %(message)s")


def _fail(message: str | Exception) -> NoReturn:
    if isinstance(message, KeyError):
        # str(KeyError("x")) is "'x'"; the caller's message is in args[0].
        message = str(message.args[0]) if message.args else repr(message)
    err.print(f"[red]error:[/red] {escape(str(message))}")
    raise typer.Exit(1)


def _load(targets_file: Path | None, rules_file: Path | None):
    settings = get_settings()
    try:
        targets = load_targets(targets_file or settings.targets_file)
        rules = load_rules(rules_file or settings.rules_file)
    except ConfigError as exc:
        _fail(exc)
    return targets, rules


def _repo() -> Repository:
    return Repository(get_db(get_settings()))


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    _setup_logging(verbose)


@app.command()
def version() -> None:
    """Print the version and what is registered."""
    console.print(f"IntoTheDarkness {__version__}")
    console.print(f"scrapers: {', '.join(scraper_names())}")
    console.print(f"channels: {', '.join(notify_channels())}")


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite existing config files."),
) -> None:
    """Create the data directory, database, and starter config files."""
    settings = get_settings()
    settings.ensure_dirs()
    get_db(settings, reload=True)
    console.print(f"[green]✓[/green] database at {settings.resolved_db_url()}")

    from .templates import (
        ENV_EXAMPLE,
        EXAMPLE_ENGINES,
        EXAMPLE_RULES,
        EXAMPLE_SECTORS,
        EXAMPLE_TARGETS,
    )

    for path, content in (
        (settings.targets_file, EXAMPLE_TARGETS),
        (settings.rules_file, EXAMPLE_RULES),
        (settings.sectors_file, EXAMPLE_SECTORS),
        (settings.engines_file, EXAMPLE_ENGINES),
        (Path(".env.example"), ENV_EXAMPLE),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not force:
            console.print(f"[yellow]·[/yellow] {path} exists, left alone")
            continue
        path.write_text(content, encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {path}")

    console.print("\nNext: edit your targets, then run [bold]itd run --dry-run[/bold].")


# --------------------------------------------------------------------------- run


@app.command()
def run(
    target: list[str] = typer.Option(None, "--target", "-t", help="Run only these targets."),
    tag: list[str] = typer.Option(None, "--tag", help="Run only targets with these tags."),
    force: bool = typer.Option(False, "--force", "-f", help="Ignore interval_minutes."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Print, don't send or persist."),
    no_notify: bool = typer.Option(False, "--no-notify", help="Detect but send nothing."),
    preview: bool = typer.Option(
        False, "--preview",
        help="Write the report as HTML instead of sending it, and print the path.",
    ),
    targets_file: Path = typer.Option(None, "--targets", help="Path to targets YAML."),
    rules_file: Path = typer.Option(None, "--rules", help="Path to rules YAML."),
) -> None:
    """Run every due target once."""
    targets, rules = _load(targets_file, rules_file)
    targets = _select(targets, target, tag)
    if not targets:
        _fail("no targets matched")

    pipeline = Pipeline(
        rules=rules,
        repo=_repo(),
        classifier=load_sectors(get_settings().sectors_file),
    )
    report = pipeline.run(
        targets, force=force, dry_run=dry_run, notify=not no_notify, preview=preview
    )

    if preview:
        previews = sorted((get_settings().data_dir / "previews").glob("report-*.html"))
        if previews:
            console.print(f"\n[green]✓[/green] report written to {previews[-1]}")
            console.print(f"  [dim]open it in a browser: file://{previews[-1]}[/dim]")

    console.print(f"\n[bold]{report.summary()}[/bold]")
    for name, message in report.errors.items():
        err.print(f"  [red]{escape(name)}[/red]: {escape(message)}")
    for channel, count in report.notified.items():
        console.print(f"  [green]sent[/green] {count} to {channel}")
    if dry_run:
        console.print("[yellow]dry run: nothing was persisted or delivered[/yellow]")

    raise typer.Exit(0 if report.ok else 2)


@app.command()
def watch(
    interval: int = typer.Option(300, "--interval", "-i", help="Seconds between sweeps."),
    targets_file: Path = typer.Option(None, "--targets", help="Path to targets YAML."),
    rules_file: Path = typer.Option(None, "--rules", help="Path to rules YAML."),
    once: bool = typer.Option(False, "--once", help="One sweep, then exit."),
) -> None:
    """Loop forever, running due targets every INTERVAL seconds.

    Targets and rules are reloaded each sweep, so config edits take effect
    without a restart.
    """
    settings = get_settings()
    pipeline = Pipeline(repo=_repo(), classifier=load_sectors(settings.sectors_file))

    while True:
        try:
            targets, rules = _load(targets_file, rules_file)
            pipeline.rules = rules
            pipeline.classifier = load_sectors(settings.sectors_file)
            report = pipeline.run(targets)
            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            console.print(f"[dim]{stamp}[/dim] {report.summary()}")
            for name, message in report.errors.items():
                err.print(f"  [red]{escape(name)}[/red]: {escape(message)}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # a bad sweep must not kill the watcher
            err.print(f"[red]sweep failed:[/red] {escape(str(exc))}")

        if once:
            return
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\nstopped")
            return


def _select(targets, names, tags):
    if names:
        wanted = set(names)
        targets = [t for t in targets if t.name in wanted]
    if tags:
        wanted = set(tags)
        targets = [t for t in targets if wanted & set(t.tags)]
    return targets


# ----------------------------------------------------------------------- targets


@targets_app.command("list")
def targets_list(
    targets_file: Path = typer.Option(None, "--targets"),
) -> None:
    """Show configured targets and when each last ran."""
    targets, _ = _load(targets_file, None)
    repo = _repo()

    table = Table(show_header=True, header_style="bold")
    for column in ("name", "scraper", "net", "every", "watch", "channels", "tags", "last run"):
        table.add_column(column)

    for t in targets:
        last = repo.last_run(t.name)
        when = "never"
        if last is not None:
            started = last.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            when = started.strftime("%Y-%m-%d %H:%M")
        name = escape(t.name)
        net = "tor" if (t.network == "tor" or is_onion(t.url)) else t.network
        table.add_row(
            name if t.enabled else f"[dim]{name} (off)[/dim]",
            escape(t.scraper),
            net,
            f"{t.interval_minutes}m",
            ",".join(k.value for k in t.watch),
            escape(",".join(t.channels) or "console"),
            escape(",".join(t.tags)),
            when,
        )
    console.print(table)


@targets_app.command("validate")
def targets_validate(
    targets_file: Path = typer.Option(None, "--targets"),
    rules_file: Path = typer.Option(None, "--rules"),
) -> None:
    """Parse the config files and report problems without fetching anything."""
    targets, rules = _load(targets_file, rules_file)
    problems: list[str] = []
    # A disabled target is a template someone has not filled in yet, so its
    # placeholders are reported but do not fail the check.
    warnings: list[str] = []

    for t in targets:
        report = problems if t.enabled else warnings
        if t.scraper not in scraper_names():
            report.append(f"{t.name}: unknown scraper {t.scraper!r}")
        if t.scraper == "css" and not t.selectors.item:
            report.append(f"{t.name}: css scraper needs selectors.item")
        if t.scraper == "json" and not t.json_path and not t.json_fields:
            report.append(f"{t.name}: json scraper needs json_path or json_fields")
        if t.scraper == "dls" and not t.selectors.item:
            report.append(f"{t.name}: dls scraper needs selectors.item")
        if is_onion(t.url):
            check = validate_onion(t.url)
            if not check.ok:
                report.append(f"{t.name}: {check.reason}")
            if t.network == "direct":
                report.append(
                    f"{t.name}: .onion target set to network 'direct' will never resolve"
                )
        if t.network == "tor" and not is_onion(t.url):
            warnings.append(f"{t.name}: clearnet URL routed over tor (deliberate?)")
        for channel in t.channels:
            if channel not in notify_channels():
                report.append(f"{t.name}: unknown channel {channel!r}")

    for rule in rules.rules:
        for channel in rule.channels:
            if channel not in notify_channels():
                problems.append(f"rule {rule.name}: unknown channel {channel!r}")
    warnings.extend(ordering_warnings(rules.rules))

    for warning in warnings:
        console.print(f"[yellow]·[/yellow] {escape(warning)}")

    if problems:
        for problem in problems:
            err.print(f"[red]✗[/red] {escape(problem)}")
        raise typer.Exit(1)

    onion = sum(1 for t in targets if is_onion(t.url))
    console.print(
        f"[green]✓[/green] {len(targets)} target(s) ({onion} over tor), "
        f"{len(rules.rules)} rule(s), all valid"
    )


@targets_app.command("test")
def targets_test(
    name: str = typer.Argument(..., help="Target to fetch."),
    limit: int = typer.Option(10, "--limit", "-l"),
    targets_file: Path = typer.Option(None, "--targets"),
) -> None:
    """Fetch one target now and print what the scraper extracted.

    Touches the network but not the database — use it while writing selectors.
    """
    targets, _ = _load(targets_file, None)
    match = next((t for t in targets if t.name == name), None)
    if match is None:
        _fail(f"no target named {name!r}")

    settings = get_settings()
    # Run the same enrichment the pipeline does, so what this prints is what a
    # real run would store — not the raw scraper output.
    pipeline = Pipeline(settings=settings, classifier=load_sectors(settings.sectors_file))
    with Fetcher(settings) as fetcher:
        try:
            items = pipeline.scrape_target(match, fetcher)
        except Exception as exc:
            _fail(f"{name}: {exc}")

    shown = redact(match.url, get_settings().redact_onion_in_logs)
    console.print(f"[green]✓[/green] {len(items)} item(s) from {escape(shown)}\n")
    for item in items[:limit]:
        console.print(f"  [bold]{escape(item.summary())}[/bold]")
        console.print(f"  [dim]key={item.key}  {escape(item.url)}[/dim]")
        if item.sector:
            console.print(f"  [dim]sector={item.sector}[/dim]")
        if item.fields:
            console.print(f"  [dim]{escape(str(item.fields))}[/dim]")
        console.print()
    if len(items) > limit:
        console.print(f"[dim]… {len(items) - limit} more[/dim]")


@targets_app.command("forget")
def targets_forget(
    name: str = typer.Argument(..., help="Target whose stored state to drop."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete stored observations so the next run re-seeds silently."""
    if not yes and not typer.confirm(f"Drop all stored observations for {name!r}?"):
        raise typer.Abort()
    count = _repo().forget_target(name)
    console.print(f"[green]✓[/green] removed {count} observation(s) for {name}")


# ---------------------------------------------------------------------- findings


@app.command()
def findings(
    since_hours: int = typer.Option(24, "--since-hours", "-s"),
    target: str = typer.Option(None, "--target", "-t"),
    min_severity: Severity = typer.Option(None, "--min-severity"),
    sector: str = typer.Option(None, "--sector", help="Only this industry sector."),
    kind: str = typer.Option(None, "--kind", help="new, changed, removed, baseline, error."),
    limit: int = typer.Option(50, "--limit", "-l"),
) -> None:
    """List findings recorded recently."""
    since = datetime.now(UTC) - timedelta(hours=since_hours)
    # Sector and kind are filtered in Python, so the database limit has to be
    # widened first — otherwise `--sector x --limit 8` means "x among the 8
    # newest" rather than "the 8 newest x", and quietly under-reports.
    fetch = limit if not (kind or sector) else max(limit * 50, 1000)
    rows = _repo().recent_findings(
        since=since, target=target, min_severity=min_severity, limit=fetch
    )
    if kind:
        rows = [r for r in rows if r.kind == kind]
    if sector:
        wanted = sector.lower()
        rows = [
            r
            for r in rows
            if str(((r.payload or {}).get("item") or {}).get("fields", {}).get("sector", ""))
            .lower()
            == wanted
        ]
    rows = rows[:limit]

    if not rows:
        console.print("[dim]no findings[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("id", "when", "target", "kind", "sev", "sector", "what"):
        table.add_column(column)
    for row in rows:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        item = ((row.payload or {}).get("item") or {})
        table.add_row(
            str(row.id),
            created.strftime("%m-%d %H:%M"),
            escape(row.target),
            row.kind,
            row.severity,
            escape(str(item.get("fields", {}).get("sector", "") or "")),
            escape((item.get("title") or row.message or "")[:60]),
        )
    console.print(table)


@app.command("notify-test")
def notify_test(
    channel: str = typer.Option("console", "--channel", "-c", help="Channel to exercise."),
) -> None:
    """Send a sample finding through one channel, to prove it is wired up."""
    settings = get_settings()
    try:
        notifier = get_notifier(channel, settings)
    except KeyError as exc:
        _fail(exc)

    ok, why = notifier.available()
    if not ok:
        _fail(f"{channel} is not configured: {why}")

    sample = [
        Finding(
            kind=FindingKind.NEW,
            target="notify-test",
            severity=Severity.MEDIUM,
            item=Item(
                key="test",
                target="notify-test",
                title="Test finding from IntoTheDarkness",
                url="https://example.com/test",
                text="If you are reading this, the channel works.",
            ),
        )
    ]
    message = Message(
        subject=render_subject(sample),
        text=render_text(sample),
        html=render_html(sample),
        findings=sample,
    )
    try:
        notifier.send(message)
    except Exception as exc:
        _fail(f"{channel} failed: {exc}")
    console.print(f"[green]✓[/green] sent a test message via {channel}")


# ------------------------------------------------------------------------- cases


def _cases() -> CaseManager:
    return CaseManager(_repo(), get_settings())


@case_app.command("open")
def case_open(
    title: str = typer.Argument(..., help="Human-readable case title."),
    slug: str = typer.Option(None, "--slug"),
    tag: list[str] = typer.Option(None, "--tag"),
) -> None:
    """Start an investigation."""
    try:
        case = _cases().open_case(title, slug, list(tag or []))
    except ValueError as exc:
        _fail(exc)
    console.print(f"[green]✓[/green] opened case [bold]{case.slug}[/bold] — {case.title}")


@case_app.command("list")
def case_list(status: str = typer.Option(None, "--status")) -> None:
    """List investigations."""
    rows = _repo().list_cases(status)
    if not rows:
        console.print("[dim]no cases[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("slug", "title", "status", "tags", "updated"):
        table.add_column(column)
    for row in rows:
        updated = row.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        table.add_row(
            escape(row.slug),
            escape(row.title),
            row.status,
            escape(",".join(row.tags)),
            updated.strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@case_app.command("note")
def case_note(
    slug: str = typer.Argument(...),
    text: str = typer.Argument(..., help="Note body; use '-' to read stdin."),
) -> None:
    """Append a note to a case."""
    body = sys.stdin.read() if text == "-" else text
    try:
        _cases().add_note(slug, body)
    except KeyError as exc:
        _fail(exc)
    console.print(f"[green]✓[/green] note added to {slug}")


@case_app.command("link")
def case_link(
    slug: str = typer.Argument(...),
    url: str = typer.Argument(...),
    note: str = typer.Option("", "--note"),
) -> None:
    """Attach a URL to a case."""
    try:
        _cases().add_link(slug, url, note)
    except KeyError as exc:
        _fail(exc)
    console.print(f"[green]✓[/green] link added to {slug}")


@case_app.command("attach")
def case_attach(
    slug: str = typer.Argument(...),
    path: Path = typer.Argument(..., help="File to copy into the case folder."),
    note: str = typer.Option("", "--note"),
) -> None:
    """Copy a file into the case folder and record its SHA-256."""
    try:
        stored = _cases().add_file(slug, path, note)
    except (KeyError, FileNotFoundError) as exc:
        _fail(exc)
    console.print(f"[green]✓[/green] stored {stored}")


@case_app.command("link-findings")
def case_link_findings(
    slug: str = typer.Argument(...),
    finding_id: list[int] = typer.Argument(..., help="Finding ids from `itd findings`."),
) -> None:
    """Attach existing findings to a case."""
    try:
        count = _cases().attach_findings(slug, list(finding_id))
    except KeyError as exc:
        _fail(exc)
    console.print(f"[green]✓[/green] attached {count} finding(s) to {slug}")


@case_app.command("show")
def case_show(slug: str = typer.Argument(...)) -> None:
    """Print a case timeline."""
    try:
        events = _cases().timeline(slug)
    except KeyError as exc:
        _fail(exc)
    case = _repo().get_case(slug)
    if case is None:  # timeline() would already have raised; keeps the type honest
        _fail(f"no case named {slug!r}")
    console.print(f"[bold]{escape(case.title)}[/bold]  [dim]({case.status})[/dim]\n")
    if not events:
        console.print("[dim]nothing on the timeline yet[/dim]")
        return
    for event in events:
        console.print(f"[dim]{event['at']:%Y-%m-%d %H:%M}[/dim]  {escape(event['label'])}")
        if event.get("body"):
            console.print(f"    {escape(event['body'])}")


@case_app.command("close")
def case_close(
    slug: str = typer.Argument(...),
    summary: str = typer.Option("", "--summary"),
) -> None:
    """Mark a case closed."""
    if _cases().close_case(slug, summary) is None:
        _fail(f"no case named {slug!r}")
    console.print(f"[green]✓[/green] closed {slug}")


@case_app.command("export")
def case_export(
    slug: str = typer.Argument(...),
    fmt: str = typer.Option("md", "--format", help="md or json."),
    out: Path = typer.Option(None, "--out"),
) -> None:
    """Write a case report to disk."""
    manager = _cases()
    try:
        path = (
            manager.export_json(slug, out)
            if fmt == "json"
            else manager.export_markdown(slug, out)
        )
    except KeyError as exc:
        _fail(exc)
    console.print(f"[green]✓[/green] wrote {path}")


if __name__ == "__main__":
    app()


# --------------------------------------------------------------------------- tor


@tor_app.command("status")
def tor_status(
    identity: bool = typer.Option(
        False, "--identity", help="Also ask check.torproject.org for the exit IP."
    ),
) -> None:
    """Report whether Tor is reachable and usable."""
    settings = get_settings()
    console.print(f"tor_enabled       {settings.tor_enabled}")
    console.print(f"socks             {settings.tor_socks_url}")
    console.print(f"control port      {settings.tor_control_port}")

    # A full SOCKS5 CONNECT, not a bare TCP connect: a tor that is still
    # bootstrapping accepts connections and then refuses to route, which looks
    # identical to a healthy proxy if you only test that the port is open.
    parsed = urlparse(settings.tor_socks_url)
    probe = probe_socks(
        host=parsed.hostname or "127.0.0.1", port=parsed.port or 9050, timeout=25.0
    )

    if probe.usable:
        console.print(f"[green]✓[/green] {escape(probe.detail)}")
    elif probe.bootstrapping:
        console.print(
            f"[yellow]·[/yellow] listening but not routing yet — "
            f"{escape(probe.detail)}"
        )
        console.print(
            "  [dim]Tor is probably still bootstrapping. Watch it with:\n"
            "    journalctl -u tor@default -f\n"
            "  Stalling at 10-25% means relay connections are blocked or "
            "throttled; use bridges.[/dim]"
        )
    else:
        console.print(f"[red]✗[/red] {escape(probe.detail)}")

    # Tor Browser's bundled tor sits on 9150 and is easy to overlook.
    if not probe.usable:
        others = [
            (port, label, other)
            for port, label, other in find_socks(timeout=8.0)
            if port != (parsed.port or 9050)
        ]
        for port, label, other in others:
            state = "usable" if other.usable else "listening, not routing"
            console.print(
                f"  [cyan]→[/cyan] found a proxy on {port} ({label}, {state}): "
                f"set [bold]ITD_TOR_SOCKS_URL=socks5://127.0.0.1:{port}[/bold]"
            )
        if not others and not probe.listening:
            # Only suggest starting Tor when nothing is listening at all;
            # a bootstrapping daemon is already started.
            console.print(
                "\n[dim]Start Tor and retry:\n"
                "  sudo apt install tor && sudo systemctl enable --now tor\n"
                "  (or) cd deploy && docker compose up -d\n"
                "  (or) open Tor Browser and use port 9150[/dim]"
            )

    fetcher = Fetcher(settings)
    try:
        ok, control_why = fetcher.tor.available()
        if ok:
            console.print("[green]✓[/green] control port library (stem) present")
        else:
            console.print(f"[yellow]·[/yellow] circuit rotation unavailable: {escape(control_why)}")

        if identity:
            if not probe.usable:
                console.print("[yellow]·[/yellow] skipping exit-IP check: no working circuit")
            else:
                ip = fetcher.tor.identity()
                console.print(
                    f"[green]✓[/green] exit IP {ip}"
                    if ip
                    else "[red]✗[/red] could not reach check.torproject.org"
                )
    finally:
        fetcher.close()

    if not probe.usable:
        raise typer.Exit(1)


@tor_app.command("check-address")
def tor_check_address(
    url: str = typer.Argument(..., help="A .onion URL or hostname to validate."),
) -> None:
    """Validate an onion address's shape before spending a timeout on it."""
    if "://" not in url:
        url = f"http://{url}"
    result = validate_onion(url)
    mark = "[green]✓[/green]" if result.ok else "[red]✗[/red]"
    version = f"v{result.version}" if result.version else "unknown version"
    console.print(f"{mark} {version}: {escape(result.reason)}")
    if not result.ok:
        raise typer.Exit(1)


# ----------------------------------------------------------------------- snapshot


@snap_app.command("fetch")
def snapshot_fetch(
    name: str = typer.Argument(..., help="Target to capture right now."),
    store: bool = typer.Option(
        False, "--store", help="Write the body to disk, not just its hash."
    ),
    targets_file: Path = typer.Option(None, "--targets"),
) -> None:
    """Fetch a target on demand and record the body, or just its digest.

    Without --store this only records a hash, which is the default posture for
    hidden services.
    """
    targets, _ = _load(targets_file, None)
    match = next((t for t in targets if t.name == name), None)
    if match is None:
        _fail(f"no target named {name!r}")

    settings = get_settings()
    with Fetcher(settings) as fetcher:
        try:
            resp = fetcher.request(
                match.method,
                match.url,
                match.headers,
                match.params,
                match.body,
                network=match.network,
            )
        except Exception as exc:
            _fail(f"{name}: {exc}")

    snapshot = SnapshotStore(settings).capture(
        target=match.name,
        url=resp.url,
        content=resp.content,
        store=store,
        content_type=resp.headers.get("content-type", ""),
        note="captured on demand",
    )
    console.print(f"[green]✓[/green] {snapshot.bytes_len} bytes over {resp.network}")
    console.print(f"  sha256  {snapshot.sha256}")
    if snapshot.stored:
        console.print(f"  stored  {snapshot.path}")
    else:
        console.print(f"  [dim]hash only{' — ' + snapshot.note if snapshot.note else ''}[/dim]")


@snap_app.command("list")
def snapshot_list(
    name: str = typer.Option(None, "--target", "-t", help="Limit to one target."),
) -> None:
    """List stored snapshots."""
    rows = SnapshotStore(get_settings()).list(name)
    if not rows:
        console.print("[dim]no stored snapshots[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    for column in ("target", "sha256", "bytes", "captured", "type"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            escape(str(row.get("target", ""))),
            str(row.get("sha256", ""))[:16],
            str(row.get("bytes", "")),
            str(row.get("captured_at", ""))[:19],
            escape(str(row.get("content_type", ""))[:30]),
        )
    console.print(table)


@snap_app.command("show")
def snapshot_show(
    name: str = typer.Argument(..., help="Target the snapshot belongs to."),
    digest: str = typer.Argument(..., help="SHA-256, or its first 16 characters."),
    out: Path = typer.Option(None, "--out", help="Write to a file instead of stdout."),
    limit: int = typer.Option(2000, "--limit", help="Characters to print."),
) -> None:
    """Recover a stored body."""
    body = SnapshotStore(get_settings()).read(name, digest)
    if body is None:
        _fail(f"no stored snapshot {digest!r} for target {name!r}")
    if out:
        out.write_bytes(body)
        console.print(f"[green]✓[/green] wrote {len(body)} bytes to {out}")
        return
    console.print(escape(body.decode("utf-8", errors="replace")[:limit]))


@snap_app.command("purge")
def snapshot_purge(
    name: str = typer.Option(None, "--target", "-t", help="Limit to one target."),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete stored bodies. Hashes in the database are untouched."""
    scope = f"target {name!r}" if name else "ALL targets"
    if not yes and not typer.confirm(f"Delete every stored snapshot for {scope}?"):
        raise typer.Abort()
    console.print(f"[green]✓[/green] removed {SnapshotStore(get_settings()).purge(name)} file(s)")


# ------------------------------------------------------------------------ sector


@sector_app.command("list")
def sector_list() -> None:
    """Show the sector vocabulary in use."""
    classifier = load_sectors(get_settings().sectors_file)
    table = Table(show_header=True, header_style="bold")
    table.add_column("sector")
    table.add_column("keywords")
    for name in classifier.known():
        keywords = ", ".join(classifier.sectors[name])
        table.add_row(name, escape(keywords[:90] + ("…" if len(keywords) > 90 else "")))
    console.print(table)


@sector_app.command("classify")
def sector_classify(
    names: list[str] = typer.Argument(..., help="Company names to label."),
) -> None:
    """Try the classifier against names, to tune the keyword list."""
    classifier = load_sectors(get_settings().sectors_file)
    for name in names:
        console.print(f"{escape(name):<44} [bold]{classifier.classify(name)}[/bold]")


# ------------------------------------------------------------------- importing


@import_app.command("ransomwatch")
def import_ransomwatch(
    source: str = typer.Argument(
        ransomwatch.DEFAULT_SOURCE,
        help="Path or URL to a ransomwatch-format groups.json.",
    ),
    out: Path = typer.Option(None, "--out", "-o", help="Write YAML here instead of stdout."),
    scraper: str = typer.Option(
        "page", "--scraper", help="page (no selectors needed) or dls (victim names)."
    ),
    enable: bool = typer.Option(False, "--enable", help="Emit targets already enabled."),
    interval: int = typer.Option(360, "--interval", help="interval_minutes per target."),
    channel: list[str] = typer.Option(None, "--channel", help="Channels for the targets."),
    group: list[str] = typer.Option(None, "--group", "-g", help="Only these group names."),
    include_unavailable: bool = typer.Option(
        False, "--include-unavailable", help="Also import mirrors currently marked down."
    ),
    show_skipped: bool = typer.Option(False, "--show-skipped", help="List what was skipped."),
) -> None:
    """Import leak-site addresses from a ransomwatch-format groups.json.

    The source maintains which groups exist and which mirrors answer today; it
    does not carry selectors. Imported targets are therefore disabled and set to
    whole-page change detection — run `itd targets suggest` against one, paste
    the selectors, switch to `scraper: dls`, then enable it.
    """
    if scraper not in ("page", "dls"):
        _fail(f"--scraper must be 'page' or 'dls', got {scraper!r}")

    if source.startswith(("http://", "https://")):
        console.print(f"[dim]fetching {escape(source)}[/dim]")
        try:
            with Fetcher(get_settings()) as fetcher:
                data = fetcher.get(source).json()
        except Exception as exc:
            _fail(f"could not fetch {source}: {exc}")
    else:
        path = Path(source)
        if not path.exists():
            _fail(f"{path} does not exist")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _fail(f"{path} is not valid JSON: {exc}")

    try:
        groups = ransomwatch.parse_groups(data)
    except ValueError as exc:
        _fail(exc)

    report = ransomwatch.build_targets(
        groups,
        scraper=scraper,
        enabled=enable,
        interval_minutes=interval,
        channels=list(channel) if channel else None,
        include_unavailable=include_unavailable,
        only=set(group) if group else None,
    )

    problems = ransomwatch.validate_targets(report.targets)
    if problems:
        for problem in problems:
            err.print(f"[red]✗[/red] {escape(problem)}")
        _fail("generated targets did not validate")

    console.print(f"[green]✓[/green] {report.summary()}")

    if show_skipped:
        reasons: dict[str, list[str]] = {}
        for name, reason in report.skipped.items():
            reasons.setdefault(reason, []).append(name)
        for reason, names in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            console.print(f"  [yellow]{len(names):>3}[/yellow] {escape(reason)}")
            if len(names) <= 12:
                console.print(f"      [dim]{escape(', '.join(sorted(names)))}[/dim]")

    if not report.targets:
        console.print("[yellow]nothing to write[/yellow]")
        return

    text = ransomwatch.to_yaml(report.targets)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {out}")
        console.print(
            "[dim]Review it, then append to your targets.yaml. Targets are "
            "disabled until you enable them.[/dim]"
        )
    else:
        console.print()
        console.print(text)


@import_app.command("ransomwatch-posts")
def import_ransomwatch_posts(
    name: str = typer.Argument(..., help="Target to pre-seed."),
    source: str = typer.Argument(..., help="Path to a ransomwatch-format posts.json."),
    group: str = typer.Option(None, "--group", "-g", help="Only this group's posts."),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
) -> None:
    """Pre-seed a target with victims already known, so the baseline is only new ones.

    Keys are computed the same way the dls scraper computes them, so a victim
    already in the history will not be reported again when it is scraped.
    """
    path = Path(source)
    if not path.exists():
        _fail(f"{path} does not exist")
    try:
        pairs = ransomwatch.seed_keys_from_posts(path, group)
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(f"{path}: {exc}")

    console.print(f"[green]✓[/green] {len(pairs)} distinct victim(s) in {path.name}")
    for _key, title in pairs[:5]:
        console.print(f"  [dim]{escape(title[:70])}[/dim]")
    if len(pairs) > 5:
        console.print(f"  [dim]… {len(pairs) - 5} more[/dim]")

    if dry_run:
        console.print("[yellow]dry run: nothing was written[/yellow]")
        return

    from .models import Item, stable_hash

    items = [
        Item(key=stable_hash("dls", key), target=name, title=title, fields={"company": title})
        for key, title in pairs
    ]
    # Seeding into an empty target records state without emitting findings.
    findings = _repo().diff_and_record(name, items, [], report_baseline=False)
    console.print(
        f"[green]✓[/green] seeded {len(items)} observation(s) into {name} "
        f"({len(findings)} finding(s) emitted)"
    )


@targets_app.command("suggest")
def targets_suggest(
    name: str = typer.Argument(None, help="Target to fetch and analyse."),
    file: Path = typer.Option(None, "--file", "-f", help="Analyse a saved HTML file instead."),
    limit: int = typer.Option(3, "--limit", "-l", help="Candidates to show."),
    targets_file: Path = typer.Option(None, "--targets"),
) -> None:
    """Propose CSS selectors for a listing page.

    Reads the samples alongside each candidate and pick the one that looks like
    a list of organisations — the ranking is a hint, the samples are the answer.
    """
    if file:
        html = file.read_text(encoding="utf-8", errors="replace")
        origin = str(file)
    elif name:
        targets, _ = _load(targets_file, None)
        match = next((t for t in targets if t.name == name), None)
        if match is None:
            _fail(f"no target named {name!r}")
        with Fetcher(get_settings()) as fetcher:
            try:
                resp = fetcher.request(
                    match.method,
                    match.url,
                    match.headers,
                    match.params,
                    match.body,
                    network=match.network,
                )
            except Exception as exc:
                _fail(f"{name}: {exc}")
        html = resp.text
        origin = redact(match.url, get_settings().redact_onion_in_logs)
    else:
        _fail("give a target name or --file")

    candidates = suggest_selectors(html, limit=limit)
    if not candidates:
        console.print(
            "[yellow]no repeating structure found[/yellow] — the page may need "
            "JavaScript, or it may not be a listing."
        )
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] {len(candidates)} candidate(s) from {escape(origin)}\n")
    for index, candidate in enumerate(candidates, 1):
        console.print(
            f"[bold]{index}.[/bold] {candidate.count} entries  "
            f"[dim](score {candidate.score})[/dim]"
        )
        for line in candidate.as_yaml().splitlines():
            console.print(f"     {escape(line)}")
        console.print("     [dim]samples:[/dim]")
        for sample in candidate.samples[:4]:
            console.print(f"       [dim]· {escape(sample[:72])}[/dim]")
        console.print()


# ---------------------------------------------------------------------- bookmarks


def _bookmarks(path: Path | None) -> tuple[Bookmarks, Path]:
    settings = get_settings()
    resolved = path or settings.bookmarks_file
    if not resolved.exists():
        _fail(
            f"{resolved} does not exist — point --file at your bookmarks.json "
            "or set ITD_BOOKMARKS_FILE"
        )
    try:
        return Bookmarks.load(resolved), resolved
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(f"{resolved}: {exc}")


@bm_app.command("status")
def bookmarks_status(
    file: Path = typer.Option(None, "--file", "-f", help="Path to bookmarks.json."),
) -> None:
    """Summarise the curated list and flag anything unusable."""
    book, path = _bookmarks(file)
    counts = book.counts()
    console.print(f"[bold]{escape(book.title or path.name)}[/bold]")
    console.print(
        f"{counts['links']} links · {counts['onion']} onion · "
        f"{counts['clearnet']} clearnet · {counts['categories']} categories\n"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("category")
    table.add_column("links", justify="right")
    table.add_column("onion", justify="right")
    for category in book.categories:
        onion = sum(1 for link in category.links if link.is_onion)
        table.add_row(escape(category.name), str(len(category.links)), str(onion))
    console.print(table)

    broken = health.stale_v2_or_malformed(book)
    if broken:
        console.print(
            f"\n[yellow]{len(broken)} onion address(es) cannot resolve "
            "as written:[/yellow]"
        )
        for name, link, why in broken:
            console.print(f"  [dim]{escape(name)}[/dim] {escape(link.title)}: {escape(why)}")


@bm_app.command("check")
def bookmarks_check(
    file: Path = typer.Option(None, "--file", "-f"),
    onion_only: bool = typer.Option(False, "--onion-only", help="Skip clearnet entries."),
    category: str = typer.Option(None, "--category", "-c", help="Only this category."),
    out: Path = typer.Option(None, "--out", help="Write full results as JSON."),
    show_alive: bool = typer.Option(False, "--show-alive", help="List living entries too."),
) -> None:
    """Check every entry over Tor and report what is dead.

    Results are reported and optionally written to JSON; bookmarks.json is never
    modified. Expect dead entries — leak-site addresses rotate constantly.
    """
    book, path = _bookmarks(file)
    settings = get_settings()

    parsed = urlparse(settings.tor_socks_url)
    probe = probe_socks(
        host=parsed.hostname or "127.0.0.1", port=parsed.port or 9050, timeout=20.0
    )
    if not probe.usable:
        console.print(
            f"[yellow]·[/yellow] {escape(probe.detail)} — every onion entry will "
            "report dead. Run [bold]itd tor status[/bold] first."
        )
        if not typer.confirm("Check anyway?", default=False):
            raise typer.Abort()

    results: list[health.Health] = []

    def report(result: health.Health) -> None:
        marks = {
            health.ALIVE: "[green]✓[/green]",
            health.DEAD: "[red]✗[/red]",
            health.INVALID: "[red]![/red]",
            health.SKIPPED: "[dim]·[/dim]",
        }
        if result.status == health.ALIVE and not show_alive:
            return
        detail = result.detail or (result.title_seen if result.status == health.ALIVE else "")
        console.print(
            f"  {marks.get(result.status, '?')} {escape(result.title[:34]):36} "
            f"[dim]{escape(detail[:56])}[/dim]"
        )

    with Fetcher(settings) as fetcher:
        results = health.check_all(
            book, fetcher, onion_only=onion_only, category=category,
            settings=settings, progress=report,
        )

    counts = health.summarize(results)
    console.print(
        f"\n[bold]{len(results)} checked[/bold] · "
        f"[green]{counts.get(health.ALIVE, 0)} alive[/green] · "
        f"[red]{counts.get(health.DEAD, 0)} dead[/red] · "
        f"{counts.get(health.INVALID, 0)} invalid · "
        f"{counts.get(health.SKIPPED, 0)} skipped"
    )

    if out:
        out.write_text(
            json.dumps([r.to_dict() for r in results], indent=2), encoding="utf-8"
        )
        console.print(f"[green]✓[/green] wrote {out}")

    console.print(f"[dim]{path} was not modified.[/dim]")


@bm_app.command("propose")
def bookmarks_propose(
    url: list[str] = typer.Argument(None, help="Addresses to propose. Use '-' for stdin."),
    file: Path = typer.Option(None, "--file", "-f"),
    from_findings: bool = typer.Option(
        False, "--from-findings", help="Propose onions discovered while scraping."
    ),
    since_hours: int = typer.Option(168, "--since-hours", help="Window for --from-findings."),
    min_sightings: int = typer.Option(1, "--min-sightings"),
    apply: bool = typer.Option(False, "--apply", help="Write them into bookmarks.json."),
    category: str = typer.Option(None, "--category", "-c", help="Force one category."),
) -> None:
    """Propose new addresses for the list, deduplicated against what is there.

    Without --apply this only prints what it would add, so the diff can be
    reviewed before anything is committed.
    """
    book, path = _bookmarks(file)

    urls = list(url or [])
    if urls == ["-"]:
        urls = [line.strip() for line in sys.stdin if line.strip()]

    proposals: list[discover.Proposal] = []
    if urls:
        proposals.extend(discover.from_urls(urls, book))
        for candidate, reason in discover.rejected(urls, book).items():
            console.print(f"[dim]· {escape(candidate[:56])}: {escape(reason)}[/dim]")

    if from_findings:
        since = datetime.now(UTC) - timedelta(hours=since_hours)
        rows = _repo().recent_findings(since=since, limit=5000)
        proposals.extend(discover.from_findings(rows, book, min_sightings))

    if not proposals:
        console.print("[dim]nothing new to propose[/dim]")
        return

    console.print(f"\n[bold]{len(proposals)} new address(es)[/bold]\n")
    for proposal in proposals:
        target_category = category or proposal.suggested_category
        console.print(f"  [bold]{escape(proposal.suggested_title[:40])}[/bold]")
        console.print(f"    {escape(redact(proposal.url, get_settings().redact_onion_in_logs))}")
        console.print(
            f"    [dim]category: {escape(target_category)} · seen {proposal.seen}x"
            + (f" via {', '.join(sorted(proposal.sources))}" if proposal.sources else "")
            + "[/dim]"
        )

    if not apply:
        console.print("\n[yellow]dry run[/yellow] — re-run with --apply to add these")
        return

    added = 0
    for proposal in proposals:
        if book.add(category or proposal.suggested_category, proposal.to_link()):
            added += 1
    save_bookmarks(book, path)
    console.print(f"\n[green]✓[/green] added {added} link(s) to {path}")
    console.print("[dim]Review the diff, run `python3 generate.py`, then commit.[/dim]")


@bm_app.command("add")
def bookmarks_add(
    title: str = typer.Argument(..., help="Human label for the entry."),
    url: str = typer.Argument(..., help="Address to add."),
    category: str = typer.Option(None, "--category", "-c", help="Category name."),
    file: Path = typer.Option(None, "--file", "-f"),
) -> None:
    """Add one entry to the curated list, preserving its formatting."""
    book, path = _bookmarks(file)

    if book.has(url):
        _fail(f"{url} is already in the list")
    rejections = discover.rejected([url], book)
    if url in rejections:
        _fail(f"{url}: {rejections[url]}")

    from .bookmarks import guess_category

    target = category or guess_category(url, title)
    if book.category(target) is None and not category:
        _fail(f"could not infer a category for {url} — pass --category")

    book.add(target, Link(title=title, url=url))
    save_bookmarks(book, path)
    console.print(f"[green]✓[/green] added {escape(title)} to {escape(target)}")
    console.print(f"[dim]{path} updated. Run `python3 generate.py`, then commit.[/dim]")


@import_app.command("bookmarks")
def import_bookmarks(
    file: Path = typer.Option(None, "--file", "-f", help="Path to bookmarks.json."),
    out: Path = typer.Option(None, "--out", "-o", help="Write YAML here instead of stdout."),
    category: list[str] = typer.Option(None, "--category", "-c", help="Only these categories."),
    scraper: str = typer.Option("page", "--scraper", help="page or dls."),
    enable: bool = typer.Option(False, "--enable"),
    interval: int = typer.Option(360, "--interval"),
    onion_only: bool = typer.Option(True, "--onion-only/--all", help="Skip clearnet entries."),
) -> None:
    """Turn the curated list into monitoring targets.

    The list is the source of truth for what is worth watching; this projects it
    into targets. Imported targets are disabled until you enable them.
    """
    import yaml

    if scraper not in ("page", "dls"):
        _fail(f"--scraper must be 'page' or 'dls', got {scraper!r}")

    book, path = _bookmarks(file)
    wanted = {c.lower() for c in category} if category else None

    targets: list[dict] = []
    skipped = 0
    for cat, link in book.links:
        if wanted and cat.name.lower() not in wanted:
            continue
        if onion_only and not link.is_onion:
            skipped += 1
            continue
        if urlparse(link.url).scheme not in ("http", "https"):
            skipped += 1
            continue
        if link.is_onion and not validate_onion(link.url).ok:
            skipped += 1
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", link.title.lower()).strip("-")[:40] or link.host[:16]
        entry: dict = {
            "name": f"bm-{slug}",
            "url": link.url,
            "scraper": scraper,
            "network": "tor" if link.is_onion else "auto",
            "enabled": enable,
            "interval_minutes": interval,
            "watch": ["new", "removed"] if scraper == "dls" else ["changed"],
            "report_baseline": True,
            "content_mode": "hash",
            "severity": "high",
            "channels": ["email"],
            "tags": ["bookmarks", re.sub(r"[^a-z0-9]+", "-", cat.name.lower()).strip("-")],
        }
        if scraper == "dls":
            entry["selectors"] = {"item": "TODO", "title": "TODO"}
        targets.append(entry)

    # Names must be unique; disambiguate any collisions from similar titles.
    seen: dict[str, int] = {}
    for entry in targets:
        base = entry["name"]
        if base in seen:
            seen[base] += 1
            entry["name"] = f"{base}-{seen[base]}"
        else:
            seen[base] = 0

    console.print(
        f"[green]✓[/green] {len(targets)} target(s) from {path.name}; {skipped} skipped"
    )
    if not targets:
        return

    header = (
        "# Generated by `itd import bookmarks` from the curated list.\n"
        "# Disabled by default. For victim extraction, run `itd targets suggest`\n"
        "# against a live site, paste the selectors, set `scraper: dls`, enable.\n"
    )
    text = header + yaml.safe_dump(
        {"targets": targets}, sort_keys=False, default_flow_style=False, width=100
    )
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/green] wrote {out}")
    else:
        console.print()
        console.print(text)


# --------------------------------------------------------------------- discovery


def _engines(only: list[str] | None = None):
    settings = get_settings()
    engines = load_engines(settings.engines_file)
    if only:
        wanted = {name.lower() for name in only}
        engines = [e for e in engines if e.name.lower() in wanted]
    return engines


@disco_app.command("engines")
def discover_engines(
    check: bool = typer.Option(False, "--check", help="Query each engine and report."),
    engine: list[str] = typer.Option(None, "--engine", "-e", help="Limit to these."),
) -> None:
    """List the configured search engines, optionally testing each one.

    These are onion addresses and they die like any other. The list is
    configuration (`config/engines.yaml`), not a promise that any of them is up.
    """
    engines = _engines(engine)
    if not engines:
        _fail("no engines configured")

    if not check:
        table = Table(show_header=True, header_style="bold")
        for column in ("engine", "net", "selector", "host"):
            table.add_column(column)
        for item in engines:
            table.add_row(
                escape(item.name) if item.enabled else f"[dim]{escape(item.name)} (off)[/dim]",
                "tor" if item.is_onion else "clearnet",
                escape(item.result_selector or "[any link]"),
                escape(redact(item.host, get_settings().redact_onion_in_logs)),
            )
        console.print(table)
        console.print(f"\n[dim]{len(engines)} engine(s) — edit {get_settings().engines_file}[/dim]")
        return

    console.print(f"[dim]querying {len(engines)} engine(s) with a probe term…[/dim]\n")
    settings = get_settings()
    live = 0
    with Fetcher(settings) as fetcher:
        report = run_search(
            "test",
            engines,
            fetcher,
            limit=0,
            content_filter=ContentFilter(extra_block_terms(settings.engines_file)),
            progress=lambda h: console.print(
                f"  {'[green]✓[/green]' if h.ok else '[red]✗[/red]'} "
                f"{escape(h.engine):16} {escape(h.detail[:56]):58} {h.seconds:5.1f}s"
            ),
        )
        live = report.responded

    console.print(f"\n[bold]{live}/{report.query_engines} responded[/bold]")
    if live == 0:
        console.print(
            "[dim]All engines failed. Check `itd tor status` first — without a "
            "working circuit every onion engine looks dead.[/dim]"
        )
        raise typer.Exit(1)


@disco_app.command("search")
def discover_search(
    query: str = typer.Argument(..., help="What to search for. Keep it short."),
    engine: list[str] = typer.Option(None, "--engine", "-e", help="Limit to these engines."),
    limit: int = typer.Option(20, "--limit", "-l", help="Candidates to show."),
    min_engines: int = typer.Option(
        None, "--min-engines", help="Engines that must agree (default adapts)."
    ),
    allow_clearnet: bool = typer.Option(
        False, "--allow-clearnet",
        help="Let clearnet engines go direct instead of over Tor.",
    ),
    apply: bool = typer.Option(False, "--apply", help="Add candidates to bookmarks.json."),
    category: str = typer.Option(None, "--category", "-c", help="Category for --apply."),
    file: Path = typer.Option(None, "--file", "-f", help="Path to bookmarks.json."),
) -> None:
    """Search onion indexes for sites not already in the curated list.

    Results are candidates for a human to review, not facts. Ranking is by how
    many distinct engines returned the same address, because search indexes are
    spammed and one hit is not evidence.

    The query is sent to each engine's operator. It is never written to logs or
    the database.
    """
    settings = get_settings()
    engines = _engines(engine)
    if not engines:
        _fail("no engines configured")

    known: set[str] = set()
    book = None
    path = None
    if settings.bookmarks_file.exists() or file:
        book, path = _bookmarks(file)
        known = book.hosts()

    if not settings.log_search_queries:
        console.print(f"[dim]searching {len(engines)} engine(s) — query not logged[/dim]\n")
    else:
        console.print(f"[dim]searching {len(engines)} engine(s) for {escape(query)!r}[/dim]\n")

    with Fetcher(settings) as fetcher:
        report = run_search(
            query,
            engines,
            fetcher,
            limit=limit,
            min_engines=min_engines,
            content_filter=ContentFilter(extra_block_terms(settings.engines_file)),
            force_tor=not allow_clearnet,
            known_hosts=known,
            progress=lambda h: console.print(
                f"  {'[green]✓[/green]' if h.ok else '[red]✗[/red]'} "
                f"{escape(h.engine):16} {escape(h.detail[:52]):54} {h.seconds:5.1f}s"
            ),
        )

    console.print(f"\n[bold]{report.summary()}[/bold]")
    if report.withheld:
        # Reported, never printed: the count proves nothing was silently lost.
        console.print(
            f"[yellow]·[/yellow] {report.withheld} result(s) withheld by the "
            "content filter and not shown"
        )
    if known:
        console.print(f"[dim]· {len(known)} address(es) already in the list were skipped[/dim]")

    if not report.candidates:
        console.print(
            f"\n[dim]no new candidates (needed {report.threshold} engine(s) to agree)[/dim]"
        )
        return

    console.print(
        f"\n[bold]{len(report.candidates)} candidate(s)[/bold] "
        f"[dim](≥{report.threshold} engine(s) agreeing)[/dim]\n"
    )
    for candidate in report.candidates:
        console.print(f"  [bold]{escape(candidate.title[:56])}[/bold]")
        console.print(f"    {escape(redact(candidate.url, settings.redact_onion_in_logs))}")
        console.print(
            f"    [dim]{candidate.corroboration} engine(s): "
            f"{escape(', '.join(sorted(candidate.engines)))}[/dim]"
        )

    if not apply:
        console.print("\n[yellow]dry run[/yellow] — re-run with --apply to add these")
        return

    if book is None or path is None:
        _fail("--apply needs a bookmarks.json; pass --file or set ITD_BOOKMARKS_FILE")

    from .bookmarks import Link, guess_category

    added = 0
    for candidate in report.candidates:
        target = category or guess_category(candidate.url, candidate.title)
        if book.add(target, Link(title=candidate.title[:60], url=candidate.url)):
            added += 1
    save_bookmarks(book, path)
    console.print(f"\n[green]✓[/green] added {added} link(s) to {path}")
    console.print("[dim]Review the diff, run `python3 generate.py`, then commit.[/dim]")


# ------------------------------------------------------------- bundled tor


@tor_app.command("install")
def tor_install(
    version: str = typer.Option(None, "--version", help="Pin a Tor Browser version."),
    force: bool = typer.Option(False, "--force", help="Re-download even if present."),
) -> None:
    """Download the Tor Expert Bundle into the data directory.

    The same standalone tor that ships inside Tor Browser, fetched from Tor
    Project's archive and checked against their published SHA-256. No root, no
    package manager, nothing written outside `data/`.
    """
    settings = get_settings()
    try:
        result = install_tor(
            settings,
            version=version,
            force=force,
            progress=lambda msg: console.print(f"  [dim]{escape(msg)}[/dim]"),
        )
    except TorInstallError as exc:
        _fail(exc)

    console.print(f"[green]✓[/green] tor {result.version} ({result.slug}) at {result.binary}")
    if result.verified:
        console.print("[green]✓[/green] checksum verified against Tor Project's published sums")
    else:
        console.print("[yellow]·[/yellow] checksum could not be verified (archive unreachable)")
    if result.pluggable_transports:
        names = sorted(p.name for p in result.pluggable_transports.iterdir() if p.is_file())
        console.print(f"[dim]  pluggable transports: {escape(', '.join(names))}[/dim]")
    console.print("\nNext: [bold]itd tor up[/bold]")


@tor_app.command("up")
def tor_up(
    timeout: float = typer.Option(None, "--timeout", help="Seconds to wait for bootstrap."),
    bridges: str = typer.Option(
        None, "--bridges", "-b",
        help="Use built-in bridges: meek, snowflake or obfs4. Try meek first.",
    ),
    socks_port: int = typer.Option(None, "--socks-port"),
    control_port: int = typer.Option(None, "--control-port"),
) -> None:
    """Start a managed Tor and wait for it to build a circuit.

    If bootstrap stalls below 25%, the network is throttling or blocking
    connections to Tor relays. Retry with --bridges meek: it tunnels Tor inside
    ordinary HTTPS to a CDN, which usually survives exactly that.
    """
    settings = get_settings()

    if bridges:
        if bridges not in BRIDGE_PRESETS:
            _fail(f"--bridges must be one of {', '.join(BRIDGE_PRESETS)}")
        lines = bundled_bridges(settings, bridges)
        if not lines:
            _fail(
                f"no built-in {bridges} bridges found — run `itd tor install` "
                "to fetch the Expert Bundle that carries them"
            )
        settings.tor_bridges = lines
        console.print(f"[dim]using {len(lines)} built-in {bridges} bridge(s)[/dim]")

    managed = ManagedTor(settings=settings)

    already = managed.running_ports()
    if already is not None:
        console.print(
            f"[green]✓[/green] already running (socks {already[0]}, control {already[1]})"
        )
        console.print(f"  [bold]export ITD_TOR_SOCKS_URL=socks5://127.0.0.1:{already[0]}[/bold]")
        return

    found = resolve_binary(settings)
    if found is None:
        _fail("no tor binary — run `itd tor install` first")
    binary, origin = found
    console.print(f"[dim]using tor from {escape(origin)}: {escape(str(binary))}[/dim]")

    last = {"pct": -1}

    def show(percent: int, detail: str) -> None:
        # Bootstrap reports the same percentage repeatedly; only print changes.
        if percent != last["pct"]:
            last["pct"] = percent
            console.print(f"  [dim]{percent:3d}%  {escape(detail)}[/dim]")

    try:
        socks, control = managed.start(
            timeout=timeout or settings.tor_bootstrap_timeout,
            progress=show,
            socks_port=socks_port,
            control_port=control_port,
        )
    except TorLaunchError as exc:
        if not bridges:
            err.print(
                "[dim]Stalled below 25%? This network probably throttles Tor "
                "relays. Retry with: [bold]itd tor up --bridges meek[/bold][/dim]"
            )
        _fail(exc)

    console.print(f"\n[green]✓[/green] bootstrapped (socks {socks}, control {control})")
    console.print(f"  [bold]export ITD_TOR_SOCKS_URL=socks5://127.0.0.1:{socks}[/bold]")
    console.print(f"  [dim]log: {managed.log_file}[/dim]")


@tor_app.command("down")
def tor_down() -> None:
    """Stop the managed Tor."""
    managed = ManagedTor(settings=get_settings())
    if managed.stop():
        console.print("[green]✓[/green] stopped")
    else:
        console.print("[dim]nothing was running[/dim]")


@tor_app.command("where")
def tor_where() -> None:
    """Show which tor binary would be used, and whether one is bundled."""
    settings = get_settings()
    bundled = tor_installed(settings)
    console.print(
        f"bundled   {escape(str(bundled.binary))} ({bundled.version})"
        if bundled
        else "bundled   [dim]none — run `itd tor install`[/dim]"
    )
    found = resolve_binary(settings)
    if found is None:
        console.print("resolved  [red]none[/red]")
        raise typer.Exit(1)
    console.print(f"resolved  {escape(str(found[0]))} [dim](from {escape(found[1])})[/dim]")

    managed = ManagedTor(settings=settings)
    ports = managed.running_ports()
    console.print(
        f"running   socks {ports[0]}, control {ports[1]}" if ports else "running   [dim]no[/dim]"
    )
