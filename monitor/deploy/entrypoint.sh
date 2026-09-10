#!/bin/sh
# Bring up the bundled Tor, then hand over to whatever was asked for.
#
# Bootstrap is attempted directly first, then through meek bridges, because a
# network that throttles Tor relays produces a stall rather than an error and
# meek is the transport that survives it.
set -e

if [ "${ITD_TOR_AUTOSTART:-1}" = "1" ]; then
    itd tor install >/dev/null 2>&1 || itd tor install
    if ! itd tor up --timeout "${ITD_BOOTSTRAP_TIMEOUT:-120}"; then
        echo "direct bootstrap failed; retrying through meek bridges" >&2
        itd tor up --bridges meek --timeout "${ITD_BOOTSTRAP_BRIDGE_TIMEOUT:-300}"
    fi
    # Point the rest of the tool at whichever port Tor came up on.
    ITD_TOR_SOCKS_URL="socks5://127.0.0.1:$(cut -d' ' -f2 "${ITD_DATA_DIR:-/data}/tor-run/tor.pid")"
    export ITD_TOR_SOCKS_URL
    echo "tor ready on ${ITD_TOR_SOCKS_URL}" >&2
fi

exec "$@"
