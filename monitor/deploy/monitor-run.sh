#!/usr/bin/env bash
# One scheduled sweep: make sure Tor can actually route, then run the monitor.
#
# Why the health check is a real circuit test and not a pid check: a managed
# Tor was once found still running, still listening, and bootstrapped 100%,
# nine days after it started -- and completely unable to route. Stale
# consensus, dead bridge. `itd tor status` performs a full SOCKS5 CONNECT and
# exits non-zero when the handshake fails, which is the only signal that
# distinguishes a working Tor from a convincing corpse.
#
# Tor is treated as a PERSISTENT daemon. A bootstrap over meek/snowflake can
# take minutes, and the old script tore Tor down and rebuilt it every cycle
# when it was not yet routing -- which reset the progress and skipped ~80% of
# sweeps during a flaky spell, losing the morning report and staling the site.
# Now a tor that is up and still bootstrapping is left to finish across cycles;
# a rebuild happens only when the process is gone or it has been unroutable
# past the grace window, and no more often than the cooldown.
#
# Silence from this script means "ran, nothing new". Anything that breaks is
# recorded (deploy/ops_log.py) and shown in the site's Diagnostics section,
# not mailed: the machine heals what it can and keeps the record.
set -uo pipefail

# The whole script sits in one brace group so bash parses it completely before
# running a line of it. Bash otherwise reads a script as it goes, and editing
# this file while a sweep is in progress once made the running copy resume at
# a shifted offset -- a syntax error at the end of a cycle whose results were
# never logged. Replace the file atomically anyway (write, then mv).
{

PROJECT="${ITD_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="${ITD_VENV_BIN:-${PROJECT}/.venv/bin}"
LOG="${PROJECT}/data/monitor.log"
LOCK="${PROJECT}/data/monitor.lock"

SOCKS_PORT=9050
CONTROL_PORT=9051
# The .onion sites see a request every four hours on the clock face otherwise,
# which is a needlessly legible pattern. Set ITD_JITTER_SECONDS=0 to disable.
JITTER="${ITD_JITTER_SECONDS:-240}"

# Tor resilience. Multiple bridges across two transports (meek + snowflake) so
# bootstrap is not betting on a single CDN front; a fresh tor is given this
# much time to finish bootstrapping across cycles before being rebuilt, and
# rebuilds happen no more often than the cooldown.
TOR_BRIDGES="${ITD_TOR_BRIDGES:-meek,snowflake}"
TOR_STATE="${PROJECT}/data/tor.state"              # epoch of the last rebuild attempt
BOOTSTRAP_GRACE="${ITD_TOR_BOOTSTRAP_GRACE:-1800}" # 30 min: leave a bootstrapping tor alone
REBUILD_COOLDOWN="${ITD_TOR_REBUILD_COOLDOWN:-1200}" # 20 min between rebuilds
TOR_BIN_RE="${PROJECT}/data/tor/tor/tor"

# During a Tor outage the page is still republished from the stored database
# (no Tor needed) so the site never silently goes stale -- at most this often.
PUBLISH_STALE_STAMP="${PROJECT}/data/publish-stale.last"
PUBLISH_STALE_INTERVAL="${ITD_PUBLISH_STALE_MINUTES:-60}"

# Sweep hours, in US Eastern wall-clock time: every two hours 07-21 on
# weekdays, every four hours 07-19 at weekends. ITD_RUN_HOURS, if set,
# overrides both.
SCHEDULE_TZ="America/New_York"
WEEKDAY_HOURS="${ITD_RUN_HOURS_WEEKDAY:-07 09 11 13 15 17 19 21}"
WEEKEND_HOURS="${ITD_RUN_HOURS_WEEKEND:-07 11 15 19}"
if [ -n "${ITD_RUN_HOURS:-}" ]; then
    RUN_HOURS="${ITD_RUN_HOURS}"
elif [ "$(TZ="${SCHEDULE_TZ}" date +%u)" -ge 6 ]; then
    RUN_HOURS="${WEEKEND_HOURS}"
else
    RUN_HOURS="${WEEKDAY_HOURS}"
fi
# At these hours the report goes out even when nothing is new: proof of life,
# so a quiet morning cannot be mistaken for a cron that stopped firing.
DIGEST_HOURS="${ITD_DIGEST_HOURS:-07}"

# The proof-of-life digest is OWED per local day. If Tor is down at the digest
# hour, the owe survives and the next full sweep that succeeds that day sends
# it -- so a bad morning no longer costs the whole day's report.
TODAY="$(TZ="${SCHEDULE_TZ}" date +%F)"
OWED="${PROJECT}/data/digest-owed-${TODAY}"
SENT="${PROJECT}/data/digest-sent-${TODAY}"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${LOG}"; }

record() {
    "${VENV}/python" "${PROJECT}/deploy/ops_log.py" "$1" "$2" "${3:-}" >>"${LOG}" 2>&1 \
        || log "WARN could not record event: $1"
}

# Republish the public page from the stored database, no scraping, rate-limited.
# Keeps the public site current (with the Diagnostics outage banner) even while
# Tor is unreachable.
publish_stale() {
    local last
    last=$(cat "${PUBLISH_STALE_STAMP}" 2>/dev/null || echo 0)
    [ $(( $(date +%s) - last )) -ge $(( PUBLISH_STALE_INTERVAL * 60 )) ] || return 0
    if "${VENV}/itd" publish >>"${LOG}" 2>&1; then
        date +%s > "${PUBLISH_STALE_STAMP}"
        log "site republished from stored data (scraping degraded)"
    fi
}

tor_routing() { timeout 45 "${VENV}/itd" tor status >/dev/null 2>&1; }

# Between scheduled reports, a watchlist-only sweep every N minutes: scrape,
# persist nothing, alert only on watchlist vendors. Read from .env so the
# interval is configured in one place with everything else.
WATCHLIST_INTERVAL="${ITD_WATCHLIST_INTERVAL_MINUTES:-$(grep -E '^ITD_WATCHLIST_INTERVAL_MINUTES=' "${PROJECT}/.env" 2>/dev/null | cut -d= -f2 | tr -d '\r ')}"
WATCHLIST_INTERVAL="${WATCHLIST_INTERVAL:-60}"
WATCHLIST_STAMP="${PROJECT}/data/watchlist.last"

# X/Twitter feeds are polled hourly on the hour, independently of the leak
# sites' slower cadence: inside a scheduled full-sweep hour they ride that
# sweep, and on every other hour a dedicated X-only run goes at :00.
X_TARGET="${ITD_X_TARGET:-x-feeds}"
x_configured() {
    grep -q "name: ${X_TARGET}" "${PROJECT}/config/targets.yaml" 2>/dev/null \
        && grep -qE '^ITD_X_AUTH_TOKEN=.' "${PROJECT}/.env" 2>/dev/null
}

DIGEST=""
MODE="sweep"
if [ "${1:-}" = "--scheduled" ]; then
    now="$(TZ="${SCHEDULE_TZ}" date +%H)"
    minute="$(date +%M)"
    case " ${RUN_HOURS} " in
        *" ${now} "*) [ "${minute}" = "00" ] || MODE="watchlist" ;;
        *) if [ "${minute}" = "00" ] && x_configured; then MODE="xonly"; else MODE="watchlist"; fi ;;
    esac
    if [ "${MODE}" = "watchlist" ]; then
        [ -s "${PROJECT}/watchlist/vendors.txt" ] || exit 0
        last=$(cat "${WATCHLIST_STAMP}" 2>/dev/null || echo 0)
        [ $(( $(date +%s) - last )) -ge $(( WATCHLIST_INTERVAL * 60 )) ] || exit 0
    fi
    # Owe a proof-of-life digest at the digest hour (once per day), and clear
    # stale owe/sent markers from previous days.
    find "${PROJECT}/data" -maxdepth 1 -name 'digest-owed-*' -mtime +2 -delete 2>/dev/null
    find "${PROJECT}/data" -maxdepth 1 -name 'digest-sent-*' -mtime +2 -delete 2>/dev/null
    case " ${DIGEST_HOURS} " in
        *" ${now} "*) [ -f "${SENT}" ] || : > "${OWED}" ;;
    esac
    # A full sweep carries the owed digest whenever it next runs, not only at
    # the digest hour -- so a Tor outage at 07:00 still yields a report later.
    if [ "${MODE}" = "sweep" ] && [ -f "${OWED}" ] && [ ! -f "${SENT}" ]; then
        DIGEST="--digest"
    fi
elif [ "${1:-}" = "--digest" ]; then
    DIGEST="--digest"
elif [ "${1:-}" = "--watchlist" ]; then
    MODE="watchlist"
fi

cd "${PROJECT}" || exit 1

# Never let a slow bootstrap collide with the next scheduled run.
exec 9>"${LOCK}"
if ! flock -n 9; then
    log "SKIP previous sweep still running"
    exit 0
fi

if [ "${JITTER}" -gt 0 ]; then
    sleep $((RANDOM % JITTER))
fi

log "--- ${MODE} starting"

# --- Tor: keep a persistent circuit; rebuild only when truly dead -----------
healed=0
if ! tor_routing; then
    now_epoch=$(date +%s)
    last_rebuild=$(cat "${TOR_STATE}" 2>/dev/null || echo 0)
    proc_alive=0; pgrep -f "${TOR_BIN_RE}" >/dev/null 2>&1 && proc_alive=1

    # A foreign process on the SOCKS port (a distro tor after a reboot) is not
    # ours to kill from cron; name it and stop.
    if [ "${proc_alive}" -eq 0 ]; then
        squatter="$(ss -H -ltnp "sport = :${SOCKS_PORT}" 2>/dev/null | head -1)"
        if [ -n "${squatter}" ]; then
            svc="$(systemctl is-active tor@default 2>/dev/null || true)"
            log "FAIL port ${SOCKS_PORT} is held by another process -- no sweep this cycle"
            record tor-port-conflict "Port ${SOCKS_PORT} is held by another process" \
"listener: ${squatter}
system tor@default service: ${svc:-unknown}
fix: sudo systemctl disable --now tor tor@default && sudo systemctl mask tor tor@default"
            publish_stale
            exit 1
        fi
    fi

    if [ "${proc_alive}" -eq 1 ] && [ $(( now_epoch - last_rebuild )) -lt "${BOOTSTRAP_GRACE}" ]; then
        log "tor up but not routing yet; within bootstrap grace, letting it finish"
        record tor-bootstrapping "Tor is up and still bootstrapping; no sweep this cycle"
        publish_stale
        exit 0
    fi
    if [ $(( now_epoch - last_rebuild )) -lt "${REBUILD_COOLDOWN}" ]; then
        log "tor not routing; within rebuild cooldown, waiting"
        record tor-down "Tor not routing; waiting out the rebuild cooldown"
        publish_stale
        exit 1
    fi

    log "tor is not routing; rebuilding through ${TOR_BRIDGES} bridges"
    timeout 60 "${VENV}/itd" tor down >/dev/null 2>&1
    date +%s > "${TOR_STATE}"
    if timeout 450 "${VENV}/itd" tor up --bridges "${TOR_BRIDGES}" \
            --socks-port "${SOCKS_PORT}" --control-port "${CONTROL_PORT}" \
            --timeout 400 >>"${LOG}" 2>&1 \
       && tor_routing; then
        healed=1
        log "tor rebuilt; circuit established on ${SOCKS_PORT}"
    else
        # The rebuild left a bootstrapping tor running (it is not killed below
        # 100% once past the block threshold); the grace window lets it finish
        # on later cycles instead of restarting from zero.
        log "tor rebuild did not route this cycle; left running to keep bootstrapping"
        record tor-down "Tor rebuild did not route yet; left running to finish bootstrapping" \
"tor log: ${PROJECT}/data/tor-run/tor.log
bridges: ${TOR_BRIDGES}"
        publish_stale
        exit 1
    fi
fi

if [ "${healed}" -eq 1 ]; then
    record tor-recovered "Tor was not routing; rebuilt through ${TOR_BRIDGES}, circuit established"
fi

# --- the sweep itself ------------------------------------------------------
# --force because the schedule lives in one place: the cron gate above. The
# leak sites carry interval_minutes: 360, which against sweeps four hours
# apart would silently skip the middle one, so the per-target interval is
# deliberately not also a scheduler here.
if [ "${MODE}" = "watchlist" ]; then
    out="$("${VENV}/itd" run --force --watchlist-only 2>&1)"
    rc=$?
    date +%s > "${WATCHLIST_STAMP}"
elif [ "${MODE}" = "xonly" ]; then
    # Hourly X poll: relevant posts ride the regular report's X section; nothing
    # relevant means no mail, and X never sends a separate [URGENT] message.
    out="$("${VENV}/itd" run --force -t "${X_TARGET}" 2>&1)"
    rc=$?
else
    out="$("${VENV}/itd" run --force ${DIGEST} 2>&1)"
    rc=$?
fi
summary="$(printf '%s\n' "${out}" | grep -E '^[0-9]+ target' | tail -1)"
printf '%s\n' "${out}" >>"${LOG}"

case "${rc}" in
    0)
        log "OK ${summary:-run complete}"
        record sweep-ok "${MODE}: ${summary:-run complete}"
        ;;
    2)
        log "PARTIAL ${summary:-run complete} (one or more targets errored)"
        record sweep-partial "${MODE}: ${summary:-run complete} — one or more targets errored" \
            "$(printf '%s\n' "${out}" | grep -vE '^INFO' | tail -12)"
        ;;
    *)
        log "FAIL itd run exited ${rc}"
        record sweep-failed "${MODE}: itd run exited ${rc}; nothing checked this cycle" \
            "$(printf '%s\n' "${out}" | grep -vE '^INFO' | tail -12)"
        ;;
esac

# The owed proof-of-life digest was carried by this sweep: mark it sent so it
# is not repeated, and clear the owe. A partial sweep (rc 2) still counts as
# proof of life.
if [ "${DIGEST}" = "--digest" ] && { [ "${rc}" -eq 0 ] || [ "${rc}" -eq 2 ]; }; then
    : > "${SENT}"
    rm -f "${OWED}"
fi

log "--- ${MODE} finished (exit ${rc})"
exit "${rc}"
}
