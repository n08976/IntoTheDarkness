# Priority watchlist

Drop a plain-text file in this folder — `vendors.txt` is the expected name —
and every victim named on it, from any source and in any sector, triggers an
urgent alert outside the normal 06:00 / 10:00 / 14:00 sweeps.

## Format

One vendor per line. Blank lines and `#` comments are ignored.

```
# Core clinical systems
Epic Systems
Oracle Health | cerner.com | oracle.com/health

# Revenue cycle
R1 RCM | r1rcm.com
```

- The first field is the name as you know it.
- Anything after `|` is an alias: an alternate name or a domain. Domains are the
  most reliable match — leak sites spell names inconsistently
  (`Communicare Inc.(US)` vs `communicare.org`), but a domain is a domain.
- Matching is case-insensitive and ignores punctuation and corporate suffixes
  (`Inc`, `LLC`, `Ltd`, `GmbH`).

## Before you commit a list here

This repository is **public**. A committed vendor list tells anyone who reads
it which suppliers you depend on. If that matters, say so before adding the
file and the list will be read from a private location instead — the folder
and format stay the same.
