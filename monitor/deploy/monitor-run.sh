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
# Silence from this script means "ran, nothing new". Anything that breaks
# sends mail, because an unnoticed broken monitor looks exactly like a quiet
# week on the leak sites.
set -uo pipefail

PROJECT="${ITD_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV="${ITD_VENV_BIN:-${PROJECT}/.venv/bin}"
LOG="${PROJECT}/data/monitor.log"
LOCK="${PROJECT}/data/monitor.lock"

SOCKS_PORT=9050
CONTROL_PORT=9051
# The .onion sites see a request every four hours on the clock face otherwise,
# which is a needlessly legible pattern. Set ITD_JITTER_SECONDS=0 to disable.
JITTER="${ITD_JITTER_SECONDS:-240}"

# Sweep hours, in US Eastern wall-clock time.
RUN_HOURS="${ITD_RUN_HOURS:-06 10 14}"
# At these hours the report goes out even when nothing is new: proof of life,
# so a quiet morning cannot be mistaken for a cron that stopped firing.
DIGEST_HOURS="${ITD_DIGEST_HOURS:-06}"
SCHEDULE_TZ="America/New_York"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${LOG}"; }

# Cron on this box is Ubuntu's 3.0pl1, which has no CRON_TZ, and Linger=no
# rules out a systemd timer with its own timezone. So cron fires this every
# hour and the schedule lives here instead, where America/New_York follows
# EDT and EST on its own and 6am stays 6am through both.
# Between scheduled reports, a watchlist-only sweep every N minutes: scrape,
# persist nothing, alert only on watchlist vendors. Read from .env so the
# interval is configured in one place with everything else.
WATCHLIST_INTERVAL="${ITD_WATCHLIST_INTERVAL_MINUTES:-$(grep -E '^ITD_WATCHLIST_INTERVAL_MINUTES=' "${PROJECT}/.env" 2>/dev/null | cut -d= -f2 | tr -d '\r ')}"
WATCHLIST_INTERVAL="${WATCHLIST_INTERVAL:-60}"
WATCHLIST_STAMP="${PROJECT}/data/watchlist.last"

DIGEST=""
MODE="sweep"
if [ "${1:-}" = "--scheduled" ]; then
    now="$(TZ="${SCHEDULE_TZ}" date +%H)"
    minute="$(date +%M)"
    case " ${RUN_HOURS} " in
        *" ${now} "*) [ "${minute}" = "00" ] || MODE="watchlist" ;;
        *) MODE="watchlist" ;;
    esac
    if [ "${MODE}" = "watchlist" ]; then
        [ -s "${PROJECT}/watchlist/vendors.txt" ] || exit 0
        last=$(cat "${WATCHLIST_STAMP}" 2>/dev/null || echo 0)
        [ $(( $(date +%s) - last )) -ge $(( WATCHLIST_INTERVAL * 60 )) ] || exit 0
    fi
    case " ${DIGEST_HOURS} " in
        *" ${now} "*) [ "${MODE}" = "sweep" ] && DIGEST="--digest" ;;
    esac
elif [ "${1:-}" = "--digest" ]; then
    DIGEST="--digest"
elif [ "${1:-}" = "--watchlist" ]; then
    MODE="watchlist"
fi

notify() {
    "${VENV}/python" "${PROJECT}/deploy/ops_notify.py" "$1" "$2" >>"${LOG}" 2>&1 \
        || log "WARN could not send ops mail: $1"
}

cd "${PROJECT}" || exit 1

# Never let a slow meek bootstrap collide with the next scheduled run.
exec 9>"${LOCK}"
if ! flock -n 9; then
    log "SKIP previous sweep still running"
    exit 0
fi

if [ "${JITTER}" -gt 0 ]; then
    sleep $((RANDOM % JITTER))
fi

log "--- ${MODE} starting"

# --- Tor: verify, and rebuild if it cannot route ---------------------------
healed=0
if ! timeout 150 "${VENV}/itd" tor status >/dev/null 2>&1; then
    log "tor is not routing; rebuilding through meek bridges"
    timeout 60 "${VENV}/itd" tor down >/dev/null 2>&1
    if timeout 450 "${VENV}/itd" tor up --bridges meek \
            --socks-port "${SOCKS_PORT}" --control-port "${CONTROL_PORT}" \
            --timeout 400 >>"${LOG}" 2>&1 \
       && timeout 150 "${VENV}/itd" tor status >/dev/null 2>&1; then
        healed=1
        log "tor rebuilt; circuit established on ${SOCKS_PORT}"
    else
        log "FAIL tor could not be rebuilt -- no sweep this cycle"
        notify "IntoTheDarkness: Tor is down and could not be rebuilt" \
"The scheduled sweep could not start because Tor could not be brought back up.

Nothing was scraped this cycle, so an empty findings inbox right now means
nothing. Monitoring is blind until this is fixed.

  host    $(hostname)
  time    $(date -u +'%Y-%m-%d %H:%M UTC')
  log     ${LOG}
  tor log ${PROJECT}/data/tor-run/tor.log

Try by hand:
  itd tor down
  itd tor up --bridges meek --socks-port ${SOCKS_PORT} --control-port ${CONTROL_PORT}
  itd tor status"
        exit 1
    fi
fi

if [ "${healed}" -eq 1 ]; then
    notify "IntoTheDarkness: Tor recovered — good to go" \
"Tor was not routing at the start of this sweep. It has been torn down and
rebuilt through meek bridges, a circuit is established, and the sweep is
running normally again.

  host   $(hostname)
  time   $(date -u +'%Y-%m-%d %H:%M UTC')
  socks  127.0.0.1:${SOCKS_PORT}

No action needed. This message exists so that a silent recovery is not
mistaken for a monitor that never broke."
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
else
    out="$("${VENV}/itd" run --force ${DIGEST} 2>&1)"
    rc=$?
fi
summary="$(printf '%s\n' "${out}" | grep -E '^[0-9]+ target' | tail -1)"
printf '%s\n' "${out}" >>"${LOG}"

case "${rc}" in
    0)
        log "OK ${summary:-run complete}"
        ;;
    2)
        log "PARTIAL ${summary:-run complete} (one or more targets errored)"
        notify "IntoTheDarkness: sweep finished with target errors" \
"The sweep ran, but at least one target failed. Findings from the targets that
did work have been delivered as usual; anything behind the failing target was
not checked this cycle.

  host  $(hostname)
  time  $(date -u +'%Y-%m-%d %H:%M UTC')
  ${summary:-}

$(printf '%s\n' "${out}" | tail -25)"
        ;;
    *)
        log "FAIL itd run exited ${rc}"
        notify "IntoTheDarkness: sweep failed" \
"The scheduled sweep failed to complete (exit ${rc}). Nothing was checked this
cycle, so an empty findings inbox right now means nothing.

  host  $(hostname)
  time  $(date -u +'%Y-%m-%d %H:%M UTC')
  log   ${LOG}

$(printf '%s\n' "${out}" | tail -25)"
        ;;
esac

log "--- ${MODE} finished (exit ${rc})"
exit "${rc}"
