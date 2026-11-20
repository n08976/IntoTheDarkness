"""Publish the report as a web page, by pushing it to a git repository.

The page is the same report the inbox gets, wrapped in a plain document:
no product name, no owner, nothing but the results. It is written into a
checkout (ITD_SITE_DIR) as index.html, a dated copy is kept under reports/
for history, and the checkout is committed and pushed; the host deploys
what it receives. Anything that fails here is reported like any other
channel failure and never blocks the mail.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .. import diag
from .base import Message, Notifier, register
from .defang import defang_onion

log = logging.getLogger(__name__)

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex">
<title>{title} · {stamp}</title>
<style>
  body{{margin:0;background:#f9fafb;color:#111827;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
  main{{max-width:760px;margin:0 auto;padding:24px 16px 60px}}
  .top{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
        border-bottom:1px solid #e5e7eb;padding-bottom:10px;margin-bottom:18px}}
  .top h1{{font-size:17px;margin:0}}
  .top a,.top span{{font-size:12px;color:#6b7280;text-decoration:none}}
  .top a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<main>
  <div class="top"><h1>{title}</h1><span>{stamp}</span>{banner}{nav}</div>
{body}
</main>
</body>
</html>
"""

LISTING = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="robots" content="noindex">
<title>All reports</title>
<style>body{{margin:0;background:#f9fafb;color:#111827;font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px 60px}}a{{color:#1d4ed8;text-decoration:none}}a:hover{{text-decoration:underline}}</style>
</head><body><main><h1 style="font-size:17px">All reports</h1><p><a href="../">latest</a></p><ul>
{items}
</ul></main></body></html>
"""


# External article links on the public page open in a new tab, so the reader
# keeps the report open while reading. Only http(s) links are touched; in-page
# anchors (#diagnostics) and de-fanged onion links are left alone.
_EXT_LINK = re.compile(r'<a\s+href="(https?://)', re.I)


def open_links_in_new_tab(html: str) -> str:
    return _EXT_LINK.sub(r'<a target="_blank" rel="noopener noreferrer" href="\1', html)


@register
class WebNotifier(Notifier):
    """Write the report into a git checkout and push it."""

    name = "web"
    wants_digest = True

    def available(self) -> tuple[bool, str]:
        d = self.settings.site_dir
        if d is None:
            return False, "ITD_SITE_DIR is not set"
        if not Path(d).is_dir():
            return False, f"{d} is not a directory"
        return True, ""

    def send(self, message: Message) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(f"web channel unavailable: {why}")
        s = self.settings
        root = Path(s.site_dir)  # type: ignore[arg-type]
        now = datetime.now(UTC)
        stamp = now.strftime("%Y-%m-%d %H:%M UTC")

        def public(html: str) -> str:
            if s.site_defang:
                # Only hidden-service links are neutralised; clearnet links (X
                # posts, news, victim sites) stay clickable for the reader.
                html = defang_onion(html)
            # The report's own footer names the product; the page names nothing.
            html = re.sub(r"IntoTheDarkness\s*·\s*", "", html)
            # Article links open in a new tab.
            return open_links_in_new_tab(html)

        body = public(message.html or f"<pre>{message.text}</pre>")
        # The archive is rebuilt from stored data on every publish, so it is
        # what has aged out as of now, not a frozen copy. A message without
        # one (a test, a plain report) leaves any existing page in place.
        if message.archive_html:
            (root / "archive.html").write_text(
                PAGE.format(
                    title="Archive", stamp=stamp, banner="",
                    nav='<a href="./">current report</a><a href="reports/">all reports</a>',
                    body=public(message.archive_html),
                ),
                encoding="utf-8",
            )
        has_archive = (root / "archive.html").exists()

        events = diag.load(Path(s.issues_file))
        issues = diag.open_issues(events)
        body += "\n" + diag.render_html(events)
        banner = (
            f'<a href="#diagnostics" style="color:#991b1b;font-weight:600">'
            f"&#9888; {len(issues)} open issue(s)</a>" if issues else ""
        )
        nav = (
            ('<a href="archive.html">archive</a>' if has_archive else "")
            + '<a href="reports/">all reports</a><a href="#diagnostics">diagnostics</a>'
        )
        page = PAGE.format(title="Scan results", stamp=stamp, body=body, banner=banner, nav=nav)
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (root / "index.html").write_text(page, encoding="utf-8")
        # The dated copy lives one directory down, so its links climb back up.
        dated = page.replace('href="archive.html"', 'href="../archive.html"').replace(
            'href="reports/"', 'href="./"'
        )
        (reports / f"{now.strftime('%Y-%m-%d-%H%M')}.html").write_text(dated, encoding="utf-8")

        kept = sorted(reports.glob("????-??-??-????.html"), reverse=True)
        for old in kept[s.site_keep_reports :]:
            old.unlink()
        items = "\n".join(
            f'<li><a href="{p.name}">{p.stem[:10]} {p.stem[11:13]}:{p.stem[13:15]} UTC</a></li>'
            for p in kept[: s.site_keep_reports]
        )
        (reports / "index.html").write_text(LISTING.format(items=items), encoding="utf-8")

        if s.site_push and (root / ".git").exists():
            self._push(root, stamp)
            if s.site_deploy_cmd:
                out = subprocess.run(
                    s.site_deploy_cmd, shell=True, capture_output=True, text=True, timeout=180
                )
                if out.returncode != 0:
                    raise RuntimeError(
                        f"site deploy command failed: {(out.stderr or out.stdout).strip()[:300]}"
                    )
                log.info("site deploy requested")

    @staticmethod
    def _push(root: Path, stamp: str) -> None:
        def git(*args: str) -> str:
            out = subprocess.run(
                ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120
            )
            if out.returncode != 0:
                raise RuntimeError(f"git {' '.join(args)}: {(out.stderr or out.stdout).strip()[:300]}")
            return out.stdout

        git("add", "-A")
        if git("status", "--porcelain").strip():
            git("commit", "-q", "-m", f"Report {stamp}")
        # Name the remote and branch: a fresh checkout has no upstream yet.
        git("push", "-q", "-u", "origin", "HEAD")
        log.info("site published: %s", stamp)
