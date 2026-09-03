# Running with Tor

`itd` never starts or manages a Tor daemon — it expects a SOCKS proxy and gets
out of the way. Pick whichever of these fits.

## 1. Tor Browser you already have (zero install)

Tor Browser runs its own tor. **While the browser is open** it listens on
**9150**, not 9050:

```bash
export ITD_TOR_SOCKS_URL=socks5://127.0.0.1:9150
itd tor status
```

Good for ad-hoc work — `itd bookmarks check`, `itd targets suggest`. Not for
scheduled monitoring, since it dies when you close the browser. `itd tor status`
probes both ports and tells you which is live.

## 2. System daemon (best for scheduled runs)

```bash
sudo apt install tor          # Debian/Ubuntu
brew install tor              # macOS
sudo systemctl enable --now tor
itd tor status --identity
```

SOCKS lands on 9050. For circuit rotation, add to `/etc/tor/torrc`:

```
ControlPort 9051
CookieAuthentication 1
CookieAuthFileGroupReadable 1
```

then `pip install -e ".[tor]"` for `stem`.

## 3. Docker (most portable)

```bash
cd deploy
cp ../.env.example .env        # fill in SMTP
docker compose up -d
docker compose exec itd itd tor status --identity
```

The healthcheck fetches through the proxy, so `itd` only starts once Tor has a
real circuit — not merely an open port.

## Verifying it actually works

```bash
itd tor status --identity     # exit IP, via check.torproject.org
itd tor check-address <addr>  # v3 shape, no network needed
```

`itd tor status` separates three states that look alike:

| | meaning |
|---|---|
| no listener | Tor is not running, or is on another port |
| listening, not usable | running but **still bootstrapping** — wait |
| usable | a circuit exists; onion fetches will work |

The middle one is the one that wastes time. A bare TCP connect can't see it,
which is why `itd` does a full SOCKS5 CONNECT instead.

## When bootstrap stalls

Bootstrap stalling at 10–25% means the network is blocking or throttling
connections to Tor relays. Symptoms: TLS handshakes to relay ORPorts taking
minutes, or timing out, while ordinary HTTPS is fine.

Fix with bridges — obfs4 traffic doesn't look like TLS:

1. Get bridges from <https://bridges.torproject.org/>
2. `sudo apt install obfs4proxy`
3. Uncomment the bridge lines in `torrc` and paste them in
4. Restart Tor and watch: `journalctl -u tor@default -f`

Cloud VMs, corporate networks and some ISPs all do this.
