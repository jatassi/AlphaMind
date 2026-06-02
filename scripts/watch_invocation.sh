#!/usr/bin/env bash
# watch_invocation.sh — canonical §5.9 archive-watch for an AlphaMind pipeline
# invocation. Streams every milestone (ingestion, distillation, each agent
# completion, Phase-2 actions, fill integration) to stdout so the `Monitor` tool
# turns each into a notification, and self-terminates on a terminal state —
# INCLUDING one that already happened before the watch attached.
#
# Works for both daemon-driven scheduled runs and manual `--once` runs. A manual
# run does NOT publish to the SSE streams (§5.1), so this reads only the WAL DB +
# the per-invocation archive — which is identical for both run kinds.
#
# Usage (pick exactly one selector):
#   watch_invocation.sh --scheduled <run-type>      # newest scheduled fire of <run-type> after --since
#   watch_invocation.sh --reason    <substring>     # manual --once run whose --reason matches
#   watch_invocation.sh --inv       <invocation_id> # attach to a specific invocation
# Options:
#   --since <ISO8601Z>   only match invocations started after this (default: now-5min); ignored with --inv
#   --tz-offset <hours>  prod-log local-time offset from UTC, for scoping abort lines to this run
#                        (default 6 = Mountain *Daylight* Time; use 7 for MST in winter — logs are MT, §"prod logs are MT")
# Env overrides: DB, LOG, ERR, ARCHROOT (prod defaults below).
#
# Pass the whole command to the `Monitor` tool with a generous timeout (or
# persistent:true). It emits ONLY real events — no routine "still waiting" ticks;
# the lone idle exception is a one-shot scheduler-port-DOWN warning during the wait.
#
# Emitted events: ARMED, DETECTED, ALREADY-COMPLETE / ALREADY-ABORTED (at attach),
# PHASE1-COMPLETE, ACT <activity_log row> (ingestion + Phase-2 actions),
# DISTILLATION, AGENT <layer>/<name> (">>> STRATEGIST" prefixed), DISTILL(log),
# FAULT(log|err), FILLS <unprocessed transition>, ABORT, PHASE2-COMPLETE + dump.
set -u

DB=${DB:-/c/Users/jacks/AlphaMind/data/alphamind.db}
LOG=${LOG:-/c/Users/jacks/AlphaMind/logs/pipeline.log}
ERR=${ERR:-/c/Users/jacks/AlphaMind/logs/pipeline.err.log}
ARCHROOT=${ARCHROOT:-/c/Users/jacks/AlphaMind/archive}
BENIGN="ProactorBasePipeTransport|closed pipe|deallocator|ResourceWarning|WinError 121|no close frame"
TZ_OFFSET=6
SINCE=""; MODE=""; SEL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --scheduled) MODE=scheduled; SEL="${2:-}"; shift 2;;
    --reason)    MODE=reason;    SEL="${2:-}"; shift 2;;
    --inv)       MODE=inv;       SEL="${2:-}"; shift 2;;
    --since)     SINCE="${2:-}"; shift 2;;
    --tz-offset) TZ_OFFSET="${2:-}"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[ -z "$MODE" ] || [ -z "$SEL" ] && {
  echo "usage: watch_invocation.sh (--scheduled <run-type> | --reason <substr> | --inv <id>) [--since ISO] [--tz-offset H]" >&2; exit 2; }
[ -z "$SINCE" ] && SINCE=$(date -u -d '-5 minutes' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)

q() { sqlite3 "$DB" "$1" 2>/dev/null; }
sched_up() { (echo >/dev/tcp/127.0.0.1/8765) 2>/dev/null && echo UP || echo DOWN; }

# This run's start in log-local (MT) time, so abort-line greps scope to THIS run.
inv_local_start() {
  # UTC "YYYY-MM-DD HH:MM:SS" from the invocation id, shifted to log-local time via
  # epoch arithmetic. (NOT `date -d "$u -6 hours"` — GNU date reads the "-6" as a
  # timezone, not a relative offset, and shifts the wrong way.)
  local u e; u=$(echo "$INV" | sed -E 's/^inv-([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z-.*/\1-\2-\3 \4:\5:\6/')
  e=$(date -u -d "$u" +%s 2>/dev/null) || return 1
  date -u -d "@$((e - TZ_OFFSET * 3600))" +"%Y-%m-%d %H:%M:%S" 2>/dev/null
}
# True if the abort signature appears in the log at/after this run's local start.
abort_logged() {
  local t; t=$(inv_local_start); [ -z "$t" ] && return 1
  awk -v t="$t" -v pat="$ABORT_PAT" 'substr($0,1,19) >= t && $0 ~ pat {f=1} END{exit f?0:1}' "$LOG" 2>/dev/null
}
final_dump() {
  echo "--- final OPEN/PENDING positions ---"
  q "SELECT json_extract(details_json,'\$.ticker')||' '||direction||' '||json_extract(details_json,'\$.share_count')||' '||status FROM positions WHERE status IN ('OPEN','PENDING') ORDER BY 1;" | sed 's/^/POS /'
  echo "CASH $(q "SELECT current_cash_usd FROM cash_ledger;")"
  q "SELECT processing_status||'='||COUNT(*) FROM fill_records GROUP BY processing_status;" | sed 's/^/FILLS /'
}

STATE=$(mktemp); ASEEN=$(mktemp)
trap 'rm -f "$STATE" "$ASEEN"' EXIT
base_unproc=$(q "SELECT COUNT(*) FROM fill_records WHERE processing_status='unprocessed';")
base_quar=$(q "SELECT COUNT(*) FROM fill_records WHERE processing_status='quarantined';")
echo "ARMED watch mode=$MODE sel='$SEL' since=$SINCE | now=$(date -u +%Y-%m-%dT%H:%M:%SZ) | baseline unprocessed=$base_unproc quarantined=$base_quar"

# ── Phase A: resolve the invocation ──
INV=""
if [ "$MODE" = inv ]; then
  INV="$SEL"
  q "SELECT 1 FROM invocations WHERE invocation_id='$INV';" | grep -q 1 || { echo "no such invocation: $INV"; exit 1; }
else
  downstreak=0
  while [ -z "$INV" ]; do
    if [ "$MODE" = scheduled ]; then
      INV=$(q "SELECT invocation_id FROM invocations WHERE trigger_source='$SEL' AND trigger_type='scheduled' AND start_at > '$SINCE' ORDER BY start_at DESC LIMIT 1;")
    else
      INV=$(q "SELECT invocation_id FROM invocations WHERE trigger_type='manual' AND trigger_source='cli' AND trigger_reason LIKE '%$SEL%' AND start_at > '$SINCE' ORDER BY start_at DESC LIMIT 1;")
    fi
    [ -n "$INV" ] && break
    if [ "$(sched_up)" = DOWN ]; then downstreak=$((downstreak+1)); [ "$downstreak" -eq 1 ] && echo "WARN scheduler port 8765 DOWN @ $(date -u +%H:%M:%SZ)"; else downstreak=0; fi
    sleep 20
  done
fi
DAY=$(echo "$INV" | sed -E 's/^inv-([0-9]{4})([0-9]{2})([0-9]{2})T.*/\1-\2-\3/')
ASOF=$(echo "$INV" | sed -E 's/^inv-([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z-.*/\1-\2-\3T\4:\5:\6Z/')
ARCH="$ARCHROOT/$DAY/$INV"
SUF=$(echo "$INV" | sed -E 's/^inv-//')
echo "DETECTED $INV | start_at=$(q "SELECT start_at FROM invocations WHERE invocation_id='$INV';") | as_of=$ASOF | archive=$ARCH"

# Abort signature derived from this invocation's actual trigger (precise per run).
ttype=$(q "SELECT trigger_type FROM invocations WHERE invocation_id='$INV';")
tsrc=$(q "SELECT trigger_source FROM invocations WHERE invocation_id='$INV';")
if [ "$ttype" = scheduled ]; then ABORT_PAT="scheduled trigger=$tsrc failed"; else ABORT_PAT="scheduler exited with error"; fi

# ── At-attach terminal check (the gap fix) ──
# If the run already reached a terminal state before we attached, report it and
# exit instead of polling a corpse. (A watcher armed after a fast-fail abort once
# polled a dead 17:00Z run for 20 min — 2026-06-02.) For a manual run the launching
# process's exit code remains the authoritative terminal signal.
p2=$(q "SELECT COALESCE(phase2_completed_at,'') FROM invocations WHERE invocation_id='$INV';")
if [ -n "$p2" ]; then echo "ALREADY-COMPLETE @ ${p2} (terminal before attach)"; final_dump; exit 0; fi
if abort_logged; then echo "ALREADY-ABORTED $INV (\"$ABORT_PAT\" logged at/after run start; phase2 NULL) — terminal before attach"; exit 0; fi

# ── Phase B: live milestone stream to terminal ──
lbase=$(wc -c <"$LOG" 2>/dev/null || echo 0)
ebase=$(wc -c <"$ERR" 2>/dev/null || echo 0)
p1emit=0; distemit=0; prev_unproc=$base_unproc
while true; do
  if [ "$p1emit" -eq 0 ]; then
    p1=$(q "SELECT COALESCE(phase1_completed_at,'') FROM invocations WHERE invocation_id='$INV';")
    if [ -n "$p1" ]; then
      fs=$(q "SELECT COALESCE(fill_collection_summary_json,'') FROM invocations WHERE invocation_id='$INV';" | tr -d '\n' | cut -c1-220)
      echo "PHASE1-COMPLETE (ingestion) @ $p1 | fill_summary=${fs:-none}"; p1emit=1
    fi
  fi

  # ingestion + Phase-2 actions: new activity_log rows for this invocation
  q "SELECT entry_id||char(31)||'ACT '||substr(entry_at,12,8)||' '||event_type||
       CASE WHEN COALESCE(order_id,'')!='' THEN ' ord='||substr(order_id,1,32) ELSE '' END||
       CASE WHEN COALESCE(position_id,'')!='' THEN ' pos='||substr(position_id,1,22) ELSE '' END||
       ' | '||REPLACE(REPLACE(substr(COALESCE(detail_json,''),1,90),char(10),' '),char(13),' ')
     FROM activity_log WHERE invocation_id='$INV' ORDER BY entry_at;" \
  | while IFS=$'\037' read -r eid line; do
      [ -z "$eid" ] && continue
      grep -qxF "$eid" "$ASEEN" && continue
      echo "$eid" >>"$ASEEN"; echo "$line"
    done

  if [ "$distemit" -eq 0 ]; then
    dc=$(q "SELECT COUNT(*) FROM distillation_composite_state WHERE as_of='$ASOF';")
    if [ -n "$dc" ] && [ "$dc" -gt 0 ]; then echo "DISTILLATION composite_state written ($dc rows) for as_of=$ASOF"; distemit=1; fi
  fi

  # each agent: atomic metadata.json (key on path|mtime so retries re-emit)
  if [ -d "$ARCH" ]; then
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      m=$(stat -c %Y "$f" 2>/dev/null); grep -qxF "$f|$m" "$STATE" && continue
      ok=$(grep -o '"success":[^,]*' "$f" | head -1 | sed 's/.*: *//;s/[" }]//g')
      [ -z "$ok" ] && continue
      echo "$f|$m" >>"$STATE"
      a=$(basename "$(dirname "$f")"); l=$(basename "$(dirname "$(dirname "$f")")")
      w=$(grep -o '"wall_clock_seconds":[^,]*' "$f" | head -1 | sed 's/.*: *//;s/[" }]//g')
      sr=$(grep -o '"stop_reason":[^,]*' "$f" | head -1 | sed 's/.*: *//;s/[" }]//g')
      mark=""; case "$a" in *strategist*) mark=">>> STRATEGIST ";; esac
      printf '%sAGENT %s/%s success=%s wall=%ss stop=%s\n' "$mark" "$l" "$a" "$ok" "${w:-0}" "${sr:-?}"
    done < <(find "$ARCH" -name metadata.json 2>/dev/null | sort)
  fi

  # fresh log appends: distillation completion + fault/abort backstop
  aborted=0
  nl=$(wc -c <"$LOG" 2>/dev/null || echo "$lbase")
  if [ "$nl" -gt "$lbase" ]; then
    fresh=$(tail -c +$((lbase+1)) "$LOG"); lbase=$nl
    echo "$fresh" | grep -E "distillation.orchestrator run_external_distillation complete" | sed -E 's/.*orchestrator /DISTILL(log) /'
    echo "$fresh" | grep -E "Traceback|CRITICAL|\bERROR\b|RepositoryConsistency|exceeded latency budget|database is locked" | grep -vE "$BENIGN" | sed 's/^/FAULT(log) /'
    echo "$fresh" | grep -qE "$ABORT_PAT" && aborted=1
  fi
  ne=$(wc -c <"$ERR" 2>/dev/null || echo "$ebase")
  if [ "$ne" -gt "$ebase" ]; then
    fresh=$(tail -c +$((ebase+1)) "$ERR"); ebase=$ne
    echo "$fresh" | grep -iE "traceback|exception|critical|\berror\b|exceeded latency budget" | grep -vE "$BENIGN" | sed 's/^/FAULT(err) /'
    echo "$fresh" | grep -qE "$ABORT_PAT" && aborted=1
  fi

  cur_unproc=$(q "SELECT COUNT(*) FROM fill_records WHERE processing_status='unprocessed';")
  if [ -n "$cur_unproc" ] && [ "$cur_unproc" != "$prev_unproc" ]; then
    quar=$(q "SELECT COUNT(*) FROM fill_records WHERE processing_status='quarantined';")
    echo "FILLS unprocessed $prev_unproc -> $cur_unproc (quarantined now=$quar)"; prev_unproc=$cur_unproc
  fi

  [ "$aborted" -eq 1 ] && { echo "ABORT $INV (\"$ABORT_PAT\") — see FAULT lines above"; break; }

  p2=$(q "SELECT COALESCE(phase2_completed_at,'') FROM invocations WHERE invocation_id='$INV';")
  if [ -n "$p2" ]; then echo "PHASE2-COMPLETE @ $p2 -- $INV done"; final_dump; break; fi
  sleep 6
done
