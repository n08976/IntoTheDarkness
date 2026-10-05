"""Turn findings into an email body a human can skim on a phone."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

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


def _is_filing(f: Finding) -> bool:
    return bool(f.item and str(f.item.fields.get("form", "")).startswith("8-K"))


def _is_social(f: Finding) -> bool:
    return bool(f.item and f.item.fields.get("source") == "x")


def _split_social(entries: Sequence[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(posts from monitored X feeds, everything else).

    X feeds are their own section: relevant posts only (a leak-site victim or a
    watchlist vendor named in a tweet), never mixed into the victim lists below.
    """
    social = [f for f in entries if _is_social(f)]
    rest = [f for f in entries if not _is_social(f)]
    return social, rest


def _social_line(f: Finding) -> str:
    it = f.item
    if it is None:
        return f.message
    handle = it.fields.get("handle", "")
    tag = _watch_tag(f)
    if not tag and _sector_of(f) != "unknown":
        tag = f"   [{_sector_of(f)}]"
    return f"@{handle}: {it.summary()}{tag}"


def _is_recent(f: Finding, days: int, now: datetime | None = None) -> bool:
    """Within `days` of now by the date the reader sees (see dates.sort_key)."""
    now = now or datetime.now(UTC)
    if days <= 0:
        return True
    when = stamp_for(f).when or f.created_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when >= now - timedelta(days=days)


def _split_vendor_victims(
    entries: Sequence[Finding],
    max_age_days: int = 0,
    now: datetime | None = None,
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """(vendor victims, vendor SEC filings, everything else).

    The two priority sections are for what is current. A vendor listing or a
    filing older than `max_age_days` ages out into the general sections --
    still marked as a vendor or an 8-K, so the history keeps saying what it
    was, but no longer at the top of a report about this week.
    """
    now = now or datetime.now(UTC)
    hit = [f for f in entries if f.item and f.item.fields.get("watchlist")]
    rest = [f for f in entries if not (f.item and f.item.fields.get("watchlist"))]
    aged = [f for f in hit if not _is_recent(f, max_age_days, now)]
    hit = [f for f in hit if _is_recent(f, max_age_days, now)]
    filings = [f for f in hit if _is_filing(f)]
    victims = [f for f in hit if not _is_filing(f)]
    return victims, filings, rest + aged


def _watch_tag(f: Finding) -> str:
    """How an aged-out vendor entry says what it is, in the general sections."""
    if not (f.item and f.item.fields.get("watchlist")):
        return ""
    kind = "SEC 8-K" if _is_filing(f) else "VENDOR"
    return f"   [{kind}: {f.item.fields['watchlist']}]"


def _filing_line(f: Finding) -> str:
    it = f.item
    if it is None:
        return f.message
    return (
        f"{it.title}   [vendor: {it.fields.get('watchlist')}]  "
        f"{it.fields.get('form', '8-K')} items {it.fields.get('items') or '?'} "
        f"filed {it.fields.get('published', '?')} — {it.fields.get('status', '')}"
    )


def _newest_first(findings: Sequence[Finding]) -> list[Finding]:
    """Newest first by the displayed date; see dates.sort_key for why."""
    return sorted(findings, key=sort_key, reverse=True)


def _sector_of(finding: Finding) -> str:
    return (finding.item.sector if finding.item else None) or "unknown"


def _priority_first(findings: Sequence[Finding], priority: Sequence[str]) -> list[Finding]:
    """Priority sectors first, then the rest; newest first within each."""
    pri = {p.lower() for p in priority}
    top = [f for f in findings if _sector_of(f).lower() in pri]
    rest = [f for f in findings if _sector_of(f).lower() not in pri]
    return _newest_first(top) + _newest_first(rest)


def _carried(findings: Sequence[Finding], carry: Sequence[str]) -> tuple[list[Finding], int]:
    """The running list carries only these sectors; the rest are counted."""
    if not carry:
        return list(findings), 0
    keep = {c.lower() for c in carry}
    # An aged-out vendor entry stays listed whatever its sector: it was a
    # priority once and the history should still show it, marked.
    kept = [
        f for f in findings
        if _sector_of(f).lower() in keep or (f.item and f.item.fields.get("watchlist"))
    ]
    return kept, len(findings) - len(kept)


def render_digest_text(
    entries: Sequence[Finding],
    new_keys: set[str],
    target_label: str = "",
    status: str = "",
    window_days: int = 30,
    recent_days: int = 4,
    priority: Sequence[str] = ("healthcare",),
    carry: Sequence[str] = ("healthcare",),
    priority_days: int = 10,
    now: datetime | None = None,
) -> str:
    """New first, then Recent, then a compact Previous list out to window_days."""
    social, entries = _split_social(entries)
    vendors, filings, entries = _split_vendor_victims(entries, priority_days, now)
    pwin = f", LAST {priority_days} DAYS" if priority_days > 0 else ""
    new = [f for f in entries if f.item and f.item.key in new_keys]
    aged = [f for f in entries if not (f.item and f.item.key in new_keys)]
    aged = [f for f in aged if _is_recent(f, window_days, now)]   # drop beyond the cutoff
    recent_list, recent_rest = _carried(
        [f for f in aged if _is_recent(f, recent_days, now)], carry
    )
    previous_list, previous_rest = _carried(
        [f for f in aged if not _is_recent(f, recent_days, now)], carry
    )

    lines = [
        f"{len(new)} new since the last report"
        + (f" across {target_label}" if target_label else "")
        + f"; {len(recent_list)} recent, {len(previous_list)} earlier"
        + f" (last {window_days} days)."
        + (f" {len(vendors)} VENDOR VICTIM(S)." if vendors else "")
        + (f" {len(filings)} VENDOR SEC FILING(S)." if filings else ""),
        "",
    ]
    if status:
        lines += [status, ""]

    # Priority above everything else: a vendor you depend on, named on a
    # leak site. Listed once, here, and not again below.
    if vendors:
        lines += [
            f"!!  VENDOR VICTIMS — PRIORITY WATCHLIST{pwin} ({len(vendors)})",
            "!!" + "=" * 44,
            "",
        ]
        for f in _newest_first(vendors):
            vendor = f.item.fields.get("watchlist") if f.item else ""
            group = f.item.fields.get("group") if f.item else ""
            lines.append(
                f"  !! {f.item.summary() if f.item else f.message}"
                f"   [vendor: {vendor}]" + (f"  by {group}" if group else "")
            )
            lines += _links(f)
        lines.append("")

    # Directly beneath: vendors that told the SEC about a cyber incident.
    if filings:
        lines += [
            f"!!  SEC 8-K CYBER FILINGS — WATCHLIST VENDORS{pwin} ({len(filings)})",
            "!!" + "=" * 44,
            "",
        ]
        for f in _newest_first(filings):
            lines.append(f"  !! {_filing_line(f)}")
            lines += _links(f)
        lines.append("")

    # Monitored X feeds: relevant posts only, their own section above the victim
    # lists so social intel is never confused with a confirmed leak-site victim.
    if social:
        lines += [
            f"MONITORED X FEEDS — RELEVANT POSTS ({len(social)})",
            "=" * 46,
            "",
        ]
        for f in _newest_first(social):
            lines.append(f"  ~ {_social_line(f)}")
            lines += _links(f)
        lines.append("")

    # One flat list per section, newest first. Sector is printed on each entry
    # rather than used as a heading: headings would split the timeline, and the
    # reader asked for a timeline.
    if new:
        pri = {p.lower() for p in priority}
        n_pri = sum(1 for f in new if _sector_of(f).lower() in pri)
        label = ", ".join(priority)
        lines += [
            f"NEW SINCE LAST REPORT ({len(new)}) — {n_pri} {label} first, then other sectors",
            "=" * 46,
            "",
        ]
        for f in _priority_first(new, priority):
            lines.append(
                f"  * [{_sector_of(f)}] {f.item.summary() if f.item else f.message}"
                + _watch_tag(f)
            )
            lines += _links(f)
        lines.append("")
    else:
        lines += ["No new entries since the last report.", ""]

    carried = ", ".join(carry) if carry else "all sectors"

    # Recent: the last `recent_days`, full detail.
    if recent_list:
        lines += [
            f"RECENT — LAST {recent_days} DAYS — {carried} ({len(recent_list)}), newest first",
            "=" * 46,
            "",
        ]
        for f in _newest_first(recent_list):
            lines.append(
                f"  - [{_sector_of(f)}] {f.item.summary() if f.item else f.message}"
                + _watch_tag(f)
            )
            lines += _links(f)
        if recent_rest:
            lines.append(f"  … plus {recent_rest} in other sectors, not listed.")
        lines.append("")

    # Previous: older than recent_days, out to the cutoff. Compact one-liners
    # with a date, no link block, so the tail stops feeling repetitive.
    if previous_list:
        lines += [
            f"PREVIOUS — {recent_days}–{window_days} DAYS — {carried} "
            f"({len(previous_list)}), newest first",
            "=" * 46,
            "",
        ]
        for f in _newest_first(previous_list):
            stamp = stamp_for(f)
            lines.append(
                f"  - {stamp.text} [{_sector_of(f)}] "
                f"{f.item.summary() if f.item else f.message}{_watch_tag(f)}"
            )
        if previous_rest:
            lines.append(f"  … plus {previous_rest} in other sectors, not listed.")
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

  {% if vendors %}
  <div style="border:3px solid #b45309;border-radius:6px;padding:2px 16px 12px;
              margin-bottom:24px;background:#fffbeb">
    <h2 style="font-size:16px;margin:12px 0 4px;color:#92400e;text-transform:uppercase;
               letter-spacing:.04em">
      &#9888; Vendor victims — priority watchlist{{ recent }} ({{ vendors|length }})</h2>
    <p style="margin:0 0 8px;color:#92400e;font-size:12px">
      Vendors you depend on, named on a leak site. Listed here and nowhere else in this
      report{% if priority_days > 0 %}; after {{ priority_days }} days an entry moves to the
      sections below, still marked <strong>VENDOR</strong>{% endif %}.</p>
    <ul style="margin:8px 0 0;padding-left:18px">
      {% for f in vendors %}{{ entry(f) }}{% endfor %}
    </ul>
  </div>
  {% endif %}

  {% if filings %}
  <div style="border:3px solid #1d4ed8;border-radius:6px;padding:2px 16px 12px;
              margin-bottom:24px;background:#eff6ff">
    <h2 style="font-size:16px;margin:12px 0 4px;color:#1e3a8a;text-transform:uppercase;
               letter-spacing:.04em">
      &#9878; SEC 8-K cyber filings — watchlist vendors{{ recent }} ({{ filings|length }})</h2>
    <p style="margin:0 0 8px;color:#1e3a8a;font-size:12px">
      Vendors that disclosed a cybersecurity incident to the SEC. Item 1.05 is a material
      incident by the company's own determination; 8.01 is incident wording under other
      events{% if priority_days > 0 %}; after {{ priority_days }} days a filing moves to the
      sections below, still marked <strong>SEC 8-K</strong>{% endif %}.</p>
    <ul style="margin:8px 0 0;padding-left:18px">
      {% for f in filings %}{{ entry(f) }}{% endfor %}
    </ul>
  </div>
  {% endif %}

  {% if social %}
  <div style="border:2px solid #4338ca;border-radius:6px;padding:2px 16px 10px;
              margin-bottom:24px;background:#eef2ff">
    <h2 style="font-size:15px;margin:12px 0 4px;color:#3730a3">
      &#128038; Monitored X feeds — relevant posts ({{ social|length }})</h2>
    <p style="margin:0 0 8px;color:#3730a3;font-size:12px">
      Posts from the watched X accounts that name a watchlist vendor or a
      healthcare target. Social intel, not confirmed leak-site victims.</p>
    <ul style="margin:8px 0 0;padding-left:18px">
      {% for f in social %}{{ entry(f) }}{% endfor %}
    </ul>
  </div>
  {% endif %}

  {% if new %}
  <div style="border:2px solid #dc2626;border-radius:6px;padding:2px 16px 10px;
              margin-bottom:24px;background:#fef2f2">
    <h2 style="font-size:15px;margin:12px 0 8px;color:#991b1b">
      New since last report ({{ new|length }})
      <span style="font-weight:normal;color:#b91c1c;font-size:12px">
        {{ priority_label }} first, then other sectors</span></h2>
    <ul style="margin:8px 0 0;padding-left:18px">
      {% for f in new %}{{ entry(f) }}{% endfor %}
    </ul>
  </div>
  {% else %}
  <p style="margin:0 0 20px;color:#6b7280">No new entries since the last report.</p>
  {% endif %}

  {% if recent_list %}
  <h2 style="font-size:15px;margin:0 0 8px;padding-bottom:4px;
             border-bottom:1px solid #e5e7eb">
    Recent — last {{ recent_days }} days — {{ carried }} ({{ recent_list|length }})
    <span style="font-weight:normal;color:#9ca3af;font-size:12px">newest first</span></h2>
  <ul style="margin:8px 0 0;padding-left:18px">
    {% for f in recent_list %}{{ entry(f) }}{% endfor %}
  </ul>
  {% if recent_rest %}
  <p style="margin:8px 0 0;color:#9ca3af;font-size:12px">
    … plus {{ recent_rest }} in other sectors, not listed.</p>
  {% endif %}
  {% endif %}

  {% if previous_list %}
  <h2 style="font-size:14px;margin:22px 0 6px;padding-bottom:4px;color:#6b7280;
             border-bottom:1px solid #e5e7eb">
    Previous — {{ recent_days }}–{{ window_days }} days — {{ carried }}
    ({{ previous_list|length }})</h2>
  <ul style="margin:6px 0 0;padding-left:18px;color:#6b7280;font-size:13px">
    {% for f in previous_list %}{{ prev(f) }}{% endfor %}
  </ul>
  {% if previous_rest %}
  <p style="margin:6px 0 0;color:#9ca3af;font-size:12px">
    … plus {{ previous_rest }} in other sectors, not listed.</p>
  {% endif %}
  {% endif %}

  <p style="margin-top:24px;color:#9ca3af;font-size:12px">
    IntoTheDarkness · {{ now }}</p>
</div>
"""
)

_ENTRY = _env.from_string(
    """<li style="margin-bottom:6px">
  <strong>{{ f.item.summary() if f.item else f.message }}</strong>
  {%- if f.item and f.item.fields.get('watchlist') %}
  <span style="font-size:11px;color:#92400e;font-weight:600;margin-left:6px">
    {{ 'SEC 8-K' if f.item.fields.get('form') else 'VENDOR' }} ·
    vendor: {{ f.item.fields['watchlist'] }}
    {%- if f.item.fields.get('group') %} · by {{ f.item.fields['group'] }}{% endif %}
    {%- if f.item.fields.get('form') %} · {{ f.item.fields['form'] }}
      items {{ f.item.fields.get('items') or '?' }}
      filed {{ f.item.fields.get('published') }}
      · {{ f.item.fields.get('status') }}{% endif %}</span>
  {%- endif %}
  {%- if f.item and f.item.fields.get('source') == 'x' %}
  <span style="font-size:11px;color:#4338ca;font-weight:600;margin-left:6px">@{{
    f.item.fields.get('handle') }}</span>
  {%- endif %}
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


_PREV = _env.from_string(
    """<li style="margin-bottom:2px">
  <span style="color:#9ca3af">{{ stamp(f).text }}</span>
  {{ f.item.summary() if f.item else f.message }}
  <span style="font-size:11px;color:#9ca3af;text-transform:uppercase">{{ sector }}</span>
  {%- if f.item and f.item.fields.get('watchlist') %}
  <span style="font-size:11px;color:#92400e">·
    {{ 'SEC 8-K' if f.item.fields.get('form') else 'VENDOR' }}:
    {{ f.item.fields['watchlist'] }}</span>
  {%- endif %}
</li>"""
)


def render_digest_html(
    entries: Sequence[Finding],
    new_keys: set[str],
    status: str = "",
    window_days: int = 30,
    recent_days: int = 4,
    priority: Sequence[str] = ("healthcare",),
    carry: Sequence[str] = ("healthcare",),
    priority_days: int = 10,
    now: datetime | None = None,
) -> str:
    from ..models import utcnow

    social, entries = _split_social(entries)
    vendors, filings, entries = _split_vendor_victims(entries, priority_days, now)
    new = [f for f in entries if f.item and f.item.key in new_keys]
    aged = [f for f in entries if not (f.item and f.item.key in new_keys)]
    aged = [f for f in aged if _is_recent(f, window_days, now)]
    recent_list, recent_rest = _carried([f for f in aged if _is_recent(f, recent_days, now)], carry)
    previous_list, previous_rest = _carried(
        [f for f in aged if not _is_recent(f, recent_days, now)], carry
    )
    return _DIGEST_HTML.render(
        total=len(entries),
        vendors=_newest_first(vendors),
        filings=_newest_first(filings),
        new=_priority_first(new, priority),
        social=_newest_first(social),
        recent_list=_newest_first(recent_list),
        previous_list=_newest_first(previous_list),
        recent_rest=recent_rest,
        previous_rest=previous_rest,
        carried=", ".join(carry) if carry else "all sectors",
        priority_label=", ".join(priority),
        priority_days=priority_days,
        recent_days=recent_days,
        recent=f", last {priority_days} days" if priority_days > 0 else "",
        # Already-rendered HTML; Markup stops the outer autoescape turning <li> to text.
        entry=lambda f: Markup(_ENTRY.render(f=f, stamp=stamp_for, sector=_sector_of(f))),
        prev=lambda f: Markup(_PREV.render(f=f, stamp=stamp_for, sector=_sector_of(f))),
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
