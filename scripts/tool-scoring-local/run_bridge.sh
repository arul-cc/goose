#!/usr/bin/env bash
# Run CowGooseService (the bridge) on this machine with its output going to a log file.
#
#   ./scripts/tool-scoring-local/run_bridge.sh up        build, stop the old one, start it
#   ./scripts/tool-scoring-local/run_bridge.sh down
#   ./scripts/tool-scoring-local/run_bridge.sh status
#   ./scripts/tool-scoring-local/run_bridge.sh log       follow the log
#
# Environment comes from $COMPLIANCECOW_SRC/local.env, read as data (it cannot be sourced: a bare
# `&` in SLACK_BOT_URL aborts the shell, and one line uses `KEY = value`), plus the two variables
# local.env lacks. Values are never printed.
#
# TOOL_SCORE_TOKEN turns on the bridge's /internal/tool-scores endpoint, which forwards goose's
# tool-call risk scores to the browser. The token is generated once into $STATE/score-token and
# shared with run_host.sh, so the goose hook can post to it.
#
# server.yaml is read from a COPY in the cache directory whose working_directory is a real local
# path, because the committed value (/home/goose) does not exist on a Mac and goose refuses it.
# The tracked file is not touched.
set -uo pipefail

SRC="${COMPLIANCECOW_SRC:-$HOME/Documents/projects/continube/ComplianceCow/src}"
SERVICE="$SRC/cowgooseservice"
STATE="$HOME/.cache/goose-scoring-local"
BIN="$STATE/cgs-new"
TOKEN_FILE="$STATE/score-token"
LOG="$STATE/cgs.log"
PORT=8080
WORKDIR="$STATE/work"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

pids_on_port() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null; }

stop_bridge() {
  local pid cmd
  for pid in $(pids_on_port); do
    cmd="$(ps -o command= -p "$pid" | cut -c1-200)"
    case "$cmd" in
      */cgs*|*cowgooseservice*) kill "$pid" ;;
      *) die "port $PORT is held by something that is not the bridge (PID $pid)" ;;
    esac
  done
  for _ in $(seq 1 20); do [ -z "$(pids_on_port)" ] && return 0; sleep 0.5; done
  die "the bridge on :$PORT did not stop"
}

case "${1:-}" in
  down) stop_bridge; echo "stopped" ;;
  status)
    pid="$(pids_on_port | head -1)"
    [ -n "$pid" ] && echo "bridge :$PORT running (PID $pid)" || echo "bridge :$PORT not running"
    ;;
  log) exec tail -f "$LOG" ;;
  up)
    [ -f "$SRC/local.env" ] || die "no $SRC/local.env"
    mkdir -p "$STATE" "$WORKDIR"
    if [ ! -s "$TOKEN_FILE" ]; then
      (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$TOKEN_FILE")
    fi
    echo "building..."
    # Build beside the live binary and rename it into place: rewriting a running executable in
    # place can get the process killed by macOS ("Code Signature Invalid").
    (cd "$SERVICE" && PATH=/usr/bin:$PATH go build -buildvcs=false -o "$BIN.new" .) || die "build failed"
    mv -f "$BIN.new" "$BIN"
    sed "s|^\\([[:space:]]*working_directory:\\).*|\\1 $WORKDIR|" "$SERVICE/configs/server.yaml" >"$STATE/server.yaml"
    grep -q "working_directory: $WORKDIR" "$STATE/server.yaml" || die "could not set working_directory in the config copy"
    stop_bridge
    : >"$LOG"; chmod 600 "$LOG"
    TOKEN_FILE="$TOKEN_FILE" SRC="$SRC" SERVICE="$SERVICE" BIN="$BIN" LOG="$LOG" CFG="$STATE/server.yaml" PORT="$PORT" python3 - <<'PY'
import os, re, subprocess
env = dict(os.environ)
loaded = 0
for line in open(os.path.join(os.environ["SRC"], "local.env"), encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    key = re.sub(r"^export\s+", "", key.strip())
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        continue
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    env[key] = value
    loaded += 1
env["COW_GOOSE_SERVICE_PORT_NUMBER"] = os.environ["PORT"]
env["COWGOOSE_USE_ACP"] = "1"
env["GOOSE_CONFIG_PATH"] = os.environ["CFG"]
env["TOOL_SCORE_TOKEN"] = open(os.environ["TOKEN_FILE"]).read().strip()
log = open(os.environ["LOG"], "ab")
proc = subprocess.Popen([os.environ["BIN"]], cwd=os.environ["SERVICE"], env=env, stdout=log, stderr=log,
                        stdin=subprocess.DEVNULL, start_new_session=True)
print(f"started PID {proc.pid}; {loaded} variables read from local.env")
PY
    for _ in $(seq 1 40); do [ -n "$(pids_on_port)" ] && break; sleep 0.5; done
    [ -n "$(pids_on_port)" ] || { echo "--- last log lines"; tail -15 "$LOG" | cut -c1-200; die "the bridge did not start listening on :$PORT"; }
    echo "up on :$PORT; log: $LOG"
    ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
