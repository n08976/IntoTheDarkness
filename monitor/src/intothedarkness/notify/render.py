"""Turn findings into an email body a human can skim on a phone."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from jinja2 import Environment, select_autoescape

from ..models import Finding, FindingKind, Severity

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


def render_subject(findings: Sequence[Finding]) -> str:
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
            if f.item and f.item.url:
                lines.append(f"      {f.item.url}")
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
