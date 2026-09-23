# Priority watchlist

Drop `vendors.txt` in this folder: one vendor name per line, nothing else.
Every victim whose name matches, from any source and in any sector, triggers
an urgent alert outside the normal 06:00 / 10:00 / 14:00 sweeps.

```
Epic Systems
Oracle Health
R1 RCM
```

Blank lines and lines starting with `#` are ignored. Matching is
case-insensitive and ignores punctuation and corporate suffixes
(`Inc`, `LLC`, `Ltd`, `GmbH`), so `R1 RCM` matches `R1 RCM, Inc.` on a
leak site.

## Before you commit a list here

This repository is **public**. A committed vendor list tells anyone who reads
it which suppliers you depend on. If that matters, say so before adding the
file and the list will be read from a private location instead.
