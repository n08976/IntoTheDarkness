"""Turn findings into an email body a human can skim on a phone."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from jinja2 import Environment, select_autoescape
from markupsafe import Markup

from ..models import Finding, FindingKind, Severity
from .dates import sort_key, stamp_for

_env = Environment(autoescape=select_autoescape(["html"]))

SEVERITY_COLOR = {
    Severity.INFO: "#6b7280",
    Severity.LOW: "#2563eb",
    Severity.MEDIUM: "#d97706",
    Severity.HIGH: "#dc2626",
    Severity.CRITICAL: "#7f1d1d",
}

_HTML = _env.from_string(
    """
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            font-size:14px;color:#111827;max-width:680px">
  <p style="margin:0 0 16px">
    <strong>{{ total }}</strong> finding{{ '' if total == 1 else 's' }}
    across <strong>{{ groups|length }}</strong> target{{ '' if groups|length == 1 else 's' }}.
  </p>
  {% for target, items in groups.items() %}
  <h3 style="margin:20px 0 8px;font-size:15px;border-bottom:1px solid #e5e7eb;
             padding-bottom:4px">{{ target }}</h3>
  <ul style="margin:0;padding-left:18px">
    {% for f in items %}
    <li style="margin-bottom:10px">
      <span style="display:inline-block;padding:1px 6px;border-radius:3px;
                   font-size:11px;text-transform:uppercase;letter-spacing:.03em;
                   color:#fff;background:{{ colors[f.severity] }}">{{ f.kind.value }}</span>
      {% if f.item and f.item.url %}
        <a href="{{ f.item.url }}" style="color:#1d4ed8;text-decoration:none">
          {{ f.item.summary() }}</a>
      {% else %}
        {{ f.item.summary() if f.item else f.message }}
      {% endif %}
      {% if f.rule %}
        <div style="color:#6b7280;font-size:12px">rule: {{ f.rule }}</div>
      {% endif %}
      {% if f.item and f.item.text and f.item.text != f.item.title %}
        <div style="color:#4b5563;font-size:13px;margin-top:2px">
          {{ f.item.text[:280] }}{{ '…' if f.item.text|length > 280 }}</div>
      {% endif %}
    </li>
    {% endfor %}
  </ul>
  {% endfor %}
  <p style="margin-top:24px;color:#9ca3af;font-size:12px">
    IntoTheDarkness · {{ now }}</p>
</div>
"""
)


def group_by_target(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in sorted(findings, key=lambda f: (-f.severity.rank, f.target)):
        groups[f.target].append(f)
    return dict(groups)


def is_baseline(findings: Sequence[Finding]) -> bool:
    """A report is a baseline when every finding in it is one."""
    return bool(findings) and all(f.kind is FindingKind.BASELINE for f in findings)


def render_subject(findings: Sequence[Finding], new_count: int | None = None) -> str:
    """Subject line. In digest mode ``new_count`` reports how many are new."""
    if new_count is not None:
        noun = "entry" if len(findings) == 1 else "entries"
        return f"IntoTheDarkness: {len(findings)} {noun} - {new_count} new"
    return _render_subject(findings)


def _render_subject(findings: Sequence[Finding]) -> str:
    if not findings:
        return "IntoTheDarkness: no findings"
    top = max(findings, key=lambda f: f.severity.rank)
    targets = {f.target for f in findings}
    scope = next(iter(targets)) if len(targets) == 1 else f"{len(targets)} targets"

    if is_baseline(findings):
        # The first report is a full picture, not an alert; say so plainly so
        # nobody reads 200 entries as 200 new events.
        return f"IntoTheDarkness baseline: {len(findings)} entries — {scope}"

    noun = "finding" if len(findings) == 1 else "findings"
    return f"[{top.severity.value.upper()}] IntoTheDarkness: {len(findings)} {noun} — {scope}"


def group_by_sector(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    """Group by industry label, largest group first, unknown last."""
    groups: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        groups[(f.item.sector if f.item else None) or "unknown"].append(f)
    return dict(
        sorted(
            groups.items(),
            key=lambda kv: (kv[0] == "unknown", -len(kv[1]), kv[0]),
        )
    )


def _links(finding: Finding) -> list[str]:
    """The two links a reader actually wants: the leak entry and the victim."""
    lines: list[str] = [f"      {stamp_for(finding).render()}"]
    item = finding.item
    if item is None:
        return lines
    if item.url:
        lines.append(f"      leak:    {item.url}")
    website = item.fields.get("website")
    if website:
        lines.append(f"      website: {website}")
    for extra in item.fields.get("also") or []:
        lines.append(f"      also:    {extra['source']}  {extra['url']}")
    return lines


def _newest_first(findings: Sequence[Finding]) -> list[Finding]:
    """Newest first by the displayed date; see dates.sort_key for why."""
    return sorted(findings, key=sort_key, reverse=True)


def _sector_of(finding: Finding) -> str:
    return (finding.item.sector if finding.item else None) or "unknown"


def render_digest_text(
    entries: Sequence[Finding],
    new_keys: set[str],
    target_label: str = "",
    status: str = "",
    window_days: int = 60,
) -> str:
    """The full running list, with anything new since the last report first."""
    new = [f for f in entries if f.item and f.item.key in new_keys]
    running = [f for f in entries if not (f.item and f.item.key in new_keys)]

    lines = [
        f"{len(new)} new since the last report"
        + (f" across {target_label}" if target_label else "")
        + f"; {len(running)} more in the last {window_days} days.",
        "",
    ]
    if status:
        lines += [status, ""]

    # One flat list per section, newest first. Sector is printed on each entry
    # rather than used as a heading: headings would split the timeline, and the
    # reader asked for a timeline.
    if new:
        lines += [f"NEW SINCE LAST REPORT ({len(new)})", "=" * 46, ""]
        for f in _newest_first(new):
            lines.append(f"  * [{_sector_of(f)}] {f.item.summary() if f.item else f.message}")
            lines += _links(f)
        lines.append("")
    else:
        lines += ["No new entries since the last report.", ""]

    lines += [
        f"DISCOVERED IN THE LAST {window_days} DAYS ({len(running)}) — newest first",
        "=" * 46,
        "",
    ]
    for f in _newest_first(running):
        lines.append(f"  - [{_sector_of(f)}] {f.item.summary() if f.item else f.message}")
        lines += _links(f)
    lines.append("")

    return "\n".join(lines).rstrip()


def render_text(findings: Sequence[Finding]) -> str:
    if not findings:
        return "no findings"

    if is_baseline(findings):
        return _render_baseline_text(findings)

    lines: list[str] = []
    for target, items in group_by_target(findings).items():
        lines.append(f"== {target} ({len(items)})")
        for f in items:
            subject = f.item.summary() if f.item else f.message
            sector = f.item.sector if f.item else None
            label = f" [{sector}]" if sector and sector != "unknown" else ""
            lines.append(f"  [{f.severity.value}] {f.kind.value}:{label} {subject}")
            lines += _links(f)
            if f.rule:
                lines.append(f"      rule: {f.rule}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_baseline_text(findings: Sequence[Finding]) -> str:
    """A baseline is a census, so lead with counts and group by sector."""
    targets = sorted({f.target for f in findings})
    sectors = group_by_sector(findings)

    lines = [
        f"BASELINE — {len(findings)} entries currently listed "
        f"across {len(targets)} source(s).",
        "This is the starting picture. Later reports will contain changes only.",
        "",
        "By sector:",
    ]
    for sector, group in sectors.items():
        lines.append(f"  {sector:<16} {len(group)}")
    lines.append("")

    for sector, group in sectors.items():
        lines.append(f"== {sector} ({len(group)})")
        for f in sorted(group, key=lambda f: (f.item.title if f.item else "").lower()):
            lines.append(f"  - {f.item.summary() if f.item else f.message}")
        lines.append("")
    return "\n".join(lines).rstrip()


_BASELINE_HTML = _env.from_string(
    """
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            font-size:14px;color:#111827;max-width:680px">
  <p style="margin:0 0 4px"><strong>Baseline report</strong></p>
  <p style="margin:0 0 16px;color:#4b5563">
    <strong>{{ total }}</strong> entries currently listed across
    <strong>{{ targets|length }}</strong> source{{ '' if targets|length == 1 else 's' }}.
    This is the starting picture — later reports contain changes only.
  </p>
  <table style="border-collapse:collapse;margin-bottom:20px">
    {% for sector, items in sectors.items() %}
    <tr>
      <td style="padding:2px 14px 2px 0;color:#374151">{{ sector }}</td>
      <td style="padding:2px 0;text-align:right;font-variant-numeric:tabular-nums">
        <strong>{{ items|length }}</strong></td>
    </tr>
    {% endfor %}
  </table>
  {% for sector, items in sectors.items() %}
  <h3 style="margin:18px 0 6px;font-size:14px;border-bottom:1px solid #e5e7eb;
             padding-bottom:4px">{{ sector }} ({{ items|length }})</h3>
  <ul style="margin:0;padding-left:18px;color:#111827">
    {% for f in items %}
    <li style="margin-bottom:3px">
      {%- if f.item and f.item.url -%}
        <a href="{{ f.item.url }}" style="color:#1d4ed8;text-decoration:none">
          {{ f.item.summary() }}</a>
      {%- else -%}
        {{ f.item.summary() if f.item else f.message }}
      {%- endif -%}
    </li>
    {% endfor %}
  </ul>
  {% endfor %}
  <p style="margin-top:24px;color:#9ca3af;font-size:12px">
    IntoTheDarkness · {{ now }}</p>
</div>
"""
)


_DIGEST_HTML = _env.from_string(
    """
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            font-size:14px;color:#111827;max-width:720px">
  <p style="margin:0 0 6px;color:#4b5563">
    <strong style="color:{{ '#dc2626' if new|length else '#6b7280' }}">
      {{ new|length }} new</strong> since the last report;
    <strong>{{ running|length }}</strong> more in the last {{ window_days }} days.
  </p>
  {% if status %}
  <p style="margin:0 0 16px;color:#9ca3af;font-size:12px">{{ status }}</p>
  {% endif %}

  {% if new %}
  <div style="border:2px solid #dc2626;border-radius:6px;padding:2px 16px 10px;
              margin-bottom:24px;background:#fef2f2">
    <h2 style="font-size:15px;margin:12px 0 8px;color:#991b1b">
      New since last report ({{ new|length }})</h2>
    <ul style="margin:8px 0 0;padding-left:18px">
      {% for f in new %}{{ entry(f) }}{% endfor %}
    </ul>
  </div>
  {% else %}
  <p style="margin:0 0 20px;color:#6b7280">No new entries since the last report.</p>
  {% endif %}

  <h2 style="font-size:15px;margin:0 0 8px;padding-bottom:4px;
             border-bottom:1px solid #e5e7eb">
    Discovered in the last {{ window_days }} days ({{ running|length }})
    <span style="font-weight:normal;color:#9ca3af;font-size:12px">newest first</span></h2>
  <ul style="margin:8px 0 0;padding-left:18px">
    {% for f in running %}{{ entry(f) }}{% endfor %}
  </ul>

  <p style="margin-top:24px;color:#9ca3af;font-size:12px">
    IntoTheDarkness · {{ now }}</p>
</div>
"""
)

_ENTRY = _env.from_string(
    """<li style="margin-bottom:6px">
  <strong>{{ f.item.summary() if f.item else f.message }}</strong>
  <span style="font-size:11px;color:#6b7280;text-transform:uppercase;
               letter-spacing:.04em;margin-left:6px">{{ sector }}</span>
  <div style="font-size:12px;color:#6b7280;margin-top:1px">
    <span style="color:#9ca3af">{{ stamp(f).label }}</span> {{ stamp(f).text }}
  </div>
  {%- if f.item and (f.item.url or f.item.fields.get('website') or f.item.fields.get('also')) %}
  <div style="font-size:12px;margin-top:1px">
    {%- if f.item.url %}
    <a href="{{ f.item.url }}" style="color:#b91c1c;text-decoration:none">leak</a>
    {%- endif %}
    {%- if f.item.url and f.item.fields.get('website') %}
    <span style="color:#d1d5db"> · </span>
    {%- endif %}
    {%- if f.item.fields.get('website') %}
    <a href="{{ f.item.fields['website'] }}"
       style="color:#1d4ed8;text-decoration:none">{{ f.item.fields['website'] }}</a>
    {%- endif %}
    {%- for extra in f.item.fields.get('also') or [] %}
    <span style="color:#d1d5db"> · </span>
    <a href="{{ extra.url }}" style="color:#6b7280;text-decoration:none">{{ extra.source }}</a>
    {%- endfor %}
  </div>
  {%- endif %}
</li>"""
)


def render_digest_html(
    entries: Sequence[Finding],
    new_keys: set[str],
    status: str = "",
    window_days: int = 60,
) -> str:
    from ..models import utcnow

    new = [f for f in entries if f.item and f.item.key in new_keys]
    running = [f for f in entries if not (f.item and f.item.key in new_keys)]
    return _DIGEST_HTML.render(
        total=len(entries),
        new=_newest_first(new),
        running=_newest_first(running),
        # The entry is already-rendered HTML. Without Markup, autoescape on
        # the outer template turns every <li> into literal text in the mail.
        entry=lambda f: Markup(_ENTRY.render(f=f, stamp=stamp_for, sector=_sector_of(f))),
        status=status,
        window_days=window_days,
        now=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )


def render_html(findings: Sequence[Finding]) -> str:
    from ..models import utcnow

    now = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    if is_baseline(findings):
        return _BASELINE_HTML.render(
            sectors=group_by_sector(findings),
            targets=sorted({f.target for f in findings}),
            total=len(findings),
            now=now,
        )

    return _HTML.render(
        groups=group_by_target(findings),
        total=len(findings),
        colors=SEVERITY_COLOR,
        now=now,
    )
