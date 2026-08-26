# IntoTheDarkness

Curated Tor / onion and clearnet bookmarks for cyber threat intelligence and darkweb investigations — primarily threat-actor leak sites, CTI reference lists, and OSINT tooling.

Version-controlled here so the page can be fetched across devices with full history.

## Files

| File | Purpose |
|------|---------|
| `bookmarks.json` | **Source of truth.** All links, categories, and notes live here. Edit this. |
| `index.html` | Styled dashboard — search box, collapsible sections, copy-link buttons, onion/clearnet badges. Open in a browser. |
| `tor_bookmarks.html` | Tab launcher. Open in Chrome and click "Open all in tabs" (or a per-section button) to open every link in its own tab. See notes below. |
| `tor_bookmarks_import.html` | Netscape bookmark file. Import into Tor Browser / Firefox via Bookmarks → Manage Bookmarks → Import. |
| `generate.py` | Regenerates all three HTML files from `bookmarks.json`. |

### Tab launcher notes

- **Pop-ups:** the first time you click, Chrome blocks the extra tabs. Click the blocked-pop-ups icon in the address bar, choose "Always allow pop-ups from this site", then click again.
- **.onion links:** these only load if Chrome is routed through Tor (a SOCKS proxy or extension). Otherwise use Tor Browser for onion sites. The launcher opens the tabs regardless.

## Updating

1. Edit `bookmarks.json` (add/remove links, change categories or notes).
2. Regenerate the outputs:
   ```
   python3 generate.py
   ```
3. Commit and push.

Never hand-edit `index.html` or `tor_bookmarks.html` — they are generated and will be overwritten.

## Categories

Ransomware & Extortion Leak Sites · CTI Reference & Tracking Lists · Forums & Communities · Marketplaces · Directories & Search Engines · Tor Project & Infrastructure · OSINT Tools

## Operational notes

- Open `.onion` links only in Tor Browser, in an environment appropriate to your operational security.
- Onion addresses for threat-actor sites rotate frequently and go offline; verify before relying on any single address.
