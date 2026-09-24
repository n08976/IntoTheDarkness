"""What went wrong, what recovered, and what is still open.

Operational events -- Tor found dead, Tor rebuilt, a sweep that failed, a
sweep that ran clean -- are appended to a JSON-lines log instead of being
mailed. The reader's inbox holds findings; the site's Diagnostics section
and `itd diag` hold the running state of the machine, and the log is the
answer to "what happened" after the fact.

An issue is open until a later event resolves it: Tor down until Tor is
rebuilt or any sweep completes; a failed or partial sweep until a clean one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

RESOLVES: dict[str, set[str]] = {
    # an event of the key kind is resolved by any later event in the set
    "tor-down": {"tor-recovered", "sweep-ok", "sweep-partial"},
    "sweep-failed": {"sweep-ok"},
    "sweep-partial": {"sweep-ok"},
    "publish-failed": {"publish-ok"},
}
FAILURES = set(RESOLVES)


@dataclass(frozen=True, slots=True)
class Event:
    when: str          # ISO 8601, UTC
    kind: str          # tor-down, tor-recovered, sweep-ok, sweep-partial, sweep-failed, ...
    summary: str
    detail: str = ""

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.when)


def record(path: Path, kind: str, summary: str, detail: str = "") -> Event:
    when = datetime.now(UTC).isoformat(timespec="seconds")
    ev = Event(when, kind, summary.strip(), detail.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(ev)) + "\n")
    return ev


def load(path: Path, limit: int = 5000) -> list[Event]:
    if not path.exists():
        return []
    out: list[Event] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(Event(**json.loads(line)))
        except Exception:  # a torn line from a crash mid-write
            continue
    return out


def open_issues(events: list[Event]) -> list[Event]:
    """The most recent failure of each kind that nothing later resolved."""
    latest: dict[str, Event] = {}
    for ev in events:
        if ev.kind in FAILURES:
            latest[ev.kind] = ev
        for kind, resolvers in RESOLVES.items():
            if ev.kind in resolvers and kind in latest and latest[kind].at <= ev.at:
                del latest[kind]
    return sorted(latest.values(), key=lambda e: e.at)


def last_ok(events: list[Event]) -> Event | None:
    return next((e for e in reversed(events) if e.kind == "sweep-ok"), None)


def render_text(events: list[Event], recent: int = 12) -> str:
    issues = open_issues(events)
    ok = last_ok(events)
    lines = ["DIAGNOSTICS", "=" * 46, ""]
    lines.append(
        f"last clean sweep: {ok.when} — {ok.summary}" if ok else "last clean sweep: none recorded"
    )
    if issues:
        lines += ["", f"OPEN ISSUES ({len(issues)})"]
        for e in issues:
            lines.append(f"  !! {e.kind:14} since {e.when}  {e.summary}")
            if e.detail:
                lines += ["       " + ln for ln in e.detail.splitlines()[:6]]
    else:
        lines += ["", "open issues: none"]
    lines += ["", f"RECENT EVENTS (last {recent})"]
    for e in events[-recent:][::-1]:
        lines.append(f"  {e.when}  {e.kind:14} {e.summary}")
    return "\n".join(lines)


def render_html(events: list[Event], recent: int = 12) -> str:
    from html import escape

    issues = open_issues(events)
    ok = last_ok(events)
    parts = ['<section id="diagnostics" style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:14px">',
             '<h2 style="font-size:15px;margin:0 0 8px">Diagnostics</h2>']
    parts.append(
        '<p style="margin:0 0 8px;color:#6b7280;font-size:12px">Last clean sweep: '
        + (f"{escape(ok.when)} — {escape(ok.summary)}" if ok else "none recorded") + "</p>"
    )
    if issues:
        parts.append('<div style="border:2px solid #dc2626;border-radius:6px;padding:8px 14px;background:#fef2f2;margin-bottom:12px">')
        parts.append(f'<strong style="color:#991b1b">Open issues ({len(issues)})</strong><ul style="margin:6px 0 0;padding-left:18px">')
        for e in issues:
            parts.append(f"<li><code>{escape(e.kind)}</code> since {escape(e.when)} — {escape(e.summary)}"
                         + (f'<pre style="font-size:11px;color:#7f1d1d;white-space:pre-wrap;margin:4px 0 0">{escape(chr(10).join(e.detail.splitlines()[:6]))}</pre>' if e.detail else "")
                         + "</li>")
        parts.append("</ul></div>")
    else:
        parts.append('<p style="margin:0 0 8px;color:#15803d;font-size:12px">Open issues: none</p>')
    parts.append(f'<details><summary style="font-size:12px;color:#6b7280;cursor:pointer">Recent events (last {recent})</summary>'
                 '<table style="font-size:12px;border-collapse:collapse;margin-top:6px">')
    for e in events[-recent:][::-1]:
        colour = "#991b1b" if e.kind in FAILURES else "#374151"
        parts.append(f'<tr><td style="padding:2px 10px 2px 0;color:#6b7280;white-space:nowrap">{escape(e.when)}</td>'
                     f'<td style="padding:2px 10px 2px 0;color:{colour}"><code>{escape(e.kind)}</code></td>'
                     f"<td style=\"padding:2px 0\">{escape(e.summary)}</td></tr>")
    parts.append("</table></details></section>")
    return "\n".join(parts)
