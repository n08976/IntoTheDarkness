# IntoTheDarkness

Curated Tor / onion and clearnet bookmarks for cyber threat intelligence and darkweb investigations — primarily threat-actor leak sites, CTI reference lists, and OSINT tooling.

Version-controlled here so the page can be fetched across devices with full history.

## Files

| File | Purpose |
|------|---------|
| `bookmarks.json` | **Source of truth.** All links, categories, and notes live here. Edit this. |
| `index.html` | Styled dashboard — search box, collapsible sections, copy-link buttons, onion/clearnet badges. Open in a browser. |
| `tor_bookmarks.html` | Netscape bookmark file. Import into Tor Browser / Firefox via Bookmarks → Manage Bookmarks → Import. |
| `generate.py` | Regenerates both HTML files from `bookmarks.json`. |

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
