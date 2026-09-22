"""CTIWatch (ctiwatch.com), read through its public Telegram channel.

The site and its API sit behind a Cloudflare JavaScript challenge that no
non-browser request gets past, key or no key. The channel's web preview at
t.me/s/ctiwatch does not, and CTIWatch posts every victim there in two
shapes -- a card per confirmed attack, and a batch post listing several --
each carrying the victim, operator, sector, country, and a link whose UUID
is CTIWatch's own identifier for the victim. That UUID is the item key, so
a victim announced in both shapes is one item, not two.

Reads three preview pages (about sixty messages, roughly two days) so a
busy stretch between sweeps is not lost off the end of the first page.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ..models import Item, Target, stable_hash
from .base import register_function
from .fetch import Fetcher

PAGES = 3
_VICTIM_LINK = re.compile(r"https?://ctiwatch\.com/victims/([0-9a-f\-]{36})")
_COUNTRY_DOT = re.compile(r"^([A-Z]{2})\s*·$")
_DOMAIN = re.compile(r"^(?:www\.)?[a-z0-9\-]+(\.[a-z0-9\-]+)+$", re.I)
_BATCH_HEAD = re.compile(r"RANSOMWARE\s*—\s*\d+ new victims", re.I)
_COUNT = re.compile(r"^\(\d+\)$")


def _flag_to_iso(text: str) -> str:
    """Two regional-indicator symbols are a country code; nothing else is."""
    codes = [ord(ch) - 0x1F1E6 + 65 for ch in text if 0x1F1E6 <= ord(ch) <= 0x1F1FF]
    return "".join(chr(c) for c in codes) if len(codes) == 2 else ""


def _wordy(line: str) -> bool:
    return any(ch.isalnum() for ch in line)


def _ends_entry(line: str) -> bool:
    """Lines that can follow a victim but are never its sector."""
    return (
        line == "•"
        or bool(_COUNT.match(line))
        or line.startswith(("All Victims", "#", "…", "Full Intel"))
    )


def _victim_name(raw: str) -> str:
    """CTIWatch re-posts a victim as "Leak: <name>" once the data is out."""
    return raw[len("Leak:"):].strip() if raw.startswith("Leak:") else raw


def _lines(node: Tag) -> list[str]:
    return [ln.strip() for ln in node.get_text("\n", strip=True).split("\n") if ln.strip()]


def _card(lines: list[str], links: list[str], when: str) -> list[dict]:
    """One confirmed-attack card: victim, country, sector, website, group."""
    try:
        i = next(k for k, ln in enumerate(lines) if "RANSOMWARE ATTACK CONFIRMED" in ln)
    except StopIteration:
        return []
    rest = [ln for ln in lines[i + 1 :] if _wordy(ln)]
    if not rest:
        return []
    victim = _victim_name(rest[0])
    country = sector = website = group = ""
    for k, ln in enumerate(rest[1:], start=1):
        if (m := _COUNTRY_DOT.match(ln)) and not country:
            country = m.group(1)
            if k + 1 < len(rest) and not rest[k + 1].startswith(("Threat Group", "Motivation")):
                sector = rest[k + 1]
        elif _DOMAIN.match(ln) and not website:
            website = ln
        elif ln.startswith("Threat Group:"):
            group = ln.split(":", 1)[1].strip() or (rest[k + 1] if k + 1 < len(rest) else "")
            break
    uuid = next((m.group(1) for ln in links if (m := _VICTIM_LINK.search(ln))), "")
    return [dict(victim=victim, country=country, sector=sector, website=website, group=group,
                 uuid=uuid, when=when, status="attack confirmed")]


def _batch(lines: list[str], links: list[str], when: str) -> list[dict]:
    """A batch post: '🦠 Group (n)' headers, then '• victim / flag / sector' runs."""
    if not any(_BATCH_HEAD.search(ln) for ln in lines):
        return []
    victim_links = [m.group(1) for ln in links if (m := _VICTIM_LINK.search(ln))]
    out: list[dict] = []
    group = ""
    k = 0
    while k < len(lines):
        ln = lines[k]
        if _COUNT.match(ln) and k > 0:
            group = lines[k - 1]
        elif ln == "•" and k + 1 < len(lines):
            victim = _victim_name(lines[k + 1])
            country = sector = ""
            j = k + 2
            if j < len(lines) and (iso := _flag_to_iso(lines[j])):
                country = iso
                j += 1
            elif j < len(lines) and not _wordy(lines[j]):
                j += 1
            # Whatever wordy line follows the flag is the sector, unless it is
            # already the next entry, a group header, or the post's footer.
            if j < len(lines) and _wordy(lines[j]) and not _ends_entry(lines[j]):
                sector = lines[j]
            out.append(dict(victim=victim, country=country, sector=sector, website="", group=group,
                            uuid=victim_links[len(out)] if len(out) < len(victim_links) else "",
                            when=when, status="listed"))
            k += 1
        k += 1
    return out


def parse_preview(html: str, target_name: str) -> list[Item]:
    soup = BeautifulSoup(html, "lxml")
    items: dict[str, Item] = {}
    for wrap in soup.select(".tgme_widget_message_wrap"):
        text = wrap.select_one(".tgme_widget_message_text")
        if not isinstance(text, Tag):
            continue
        t = wrap.select_one("time")
        when = str(t.get("datetime") or "") if isinstance(t, Tag) else ""
        lines = _lines(text)
        links = [str(a.get("href") or "") for a in text.select("a[href]")]
        for rec in _card(lines, links, when) or _batch(lines, links, when):
            key = rec["uuid"] or stable_hash(rec["victim"].lower(), rec["group"].lower())
            keep = ("group", "sector", "country", "website", "status")
            fields = {k: v for k, v in rec.items() if k in keep and v}
            if rec["when"]:
                fields["published"] = rec["when"]
            url = f"https://ctiwatch.com/victims/{rec['uuid']}" if rec["uuid"] else ""
            # A card carries more than a batch line; let it win when both exist.
            if key not in items or rec["status"] == "attack confirmed":
                summary = " · ".join(v for v in (rec["sector"], rec["country"], rec["status"]) if v)
                items[key] = Item(
                    key=key,
                    target=target_name,
                    title=rec["victim"],
                    url=url,
                    text=summary,
                    fields=fields,
                )
    return list(items.values())


@register_function("ctiwatch")
def scrape(target: Target, fetcher: Fetcher) -> list[Item]:
    items: dict[str, Item] = {}
    url = target.url
    for _ in range(PAGES):
        resp = fetcher.request(
            "GET", url, target.headers, target.params, target.body, network=target.network
        )
        page = parse_preview(resp.text, target.name)
        for it in page:
            items.setdefault(it.key, it)
        ids = [int(m.group(1)) for m in re.finditer(r'data-post="ctiwatch/(\d+)"', resp.text)]
        if not ids:
            break
        url = f"{target.url.split('?')[0]}?before={min(ids)}"
    if not items and "tgme_widget_message" not in resp.text:
        raise ValueError(f"target {target.name!r}: {target.url} is not a Telegram channel preview")
    return list(items.values())
