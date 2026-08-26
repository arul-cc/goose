#!/usr/bin/env bash
# Run `goose serve` for the ComplianceCow setup.
#
#   ./scripts/run-goose.sh                  # foreground, port from CowGooseService
#   ./scripts/run-goose.sh --port 3284
#   ./scripts/run-goose.sh --background     # detached, logs to /tmp
#   ./scripts/run-goose.sh --big-stack      # 32MB worker stacks
#   ./scripts/run-goose.sh --release
#
# Serves ACP over WebSocket at ws://127.0.0.1:<port>/acp, unauthenticated —
# CowGooseService sends `x-secret-key` but goose ignores it in this mode. This is
# a local development posture: it binds 127.0.0.1 only. Do not use it on a shared
# or public host.
#
# The port defaults to whatever CowGooseService is configured to dial, read from
# its configs/server.yaml. That value is the contract between the two processes,
# and having them disagree presents as a hang rather than an error, so this reads
# the same file instead of hardcoding a guess.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

CGS_CONFIG="${COWGOOSE_CONFIG:-/Users/Arul/Documents/projects/continube/ComplianceCow/src/cowgooseservice/configs/server.yaml}"
PROFILE=debug
PORT=""
HOST=127.0.0.1
BACKGROUND=0
BIG_STACK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port)       PORT="$2"; shift 2 ;;
    --host)       HOST="$2"; shift 2 ;;
    --release)    PROFILE=release; shift ;;
    --background) BACKGROUND=1; shift ;;
    --big-stack)  BIG_STACK=1; shift ;;
    -h|--help)    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

BINARY="$ROOT/target/$PROFILE/goose"

if [ ! -x "$BINARY" ]; then
  echo "no binary at $BINARY — build it first:" >&2
  echo "    ./scripts/build-goose.sh${PROFILE:+ $([ "$PROFILE" = release ] && echo --release)}" >&2
  exit 1
fi

# Warn when the binary predates the sources: a fix you just made but did not
# rebuild looks exactly like a fix that did not work.
NEWEST_SRC="$(find "$ROOT/crates" -name '*.rs' -newer "$BINARY" -print -quit 2>/dev/null)"
if [ -n "$NEWEST_SRC" ]; then
  echo "warning: $BINARY is older than your sources (e.g. ${NEWEST_SRC#"$ROOT/"})."
  echo "         Rebuild with ./scripts/build-goose.sh or you will be running stale code."
  echo
fi

# --- port -------------------------------------------------------------------
if [ -z "$PORT" ]; then
  if [ -f "$CGS_CONFIG" ]; then
    PORT="$(sed -n 's/^[[:space:]]*port:[[:space:]]*"\{0,1\}\([0-9]\{1,\}\)"\{0,1\}[[:space:]]*$/\1/p' "$CGS_CONFIG" 2>/dev/null | head -1)"
    [ -n "$PORT" ] && echo "port $PORT (from CowGooseService's ${CGS_CONFIG##*/})"
  fi
fi
if [ -z "$PORT" ]; then
  PORT=3000
  echo "port $PORT (default — could not read $CGS_CONFIG)"
fi

# --- stop whatever is already there -----------------------------------------
EXISTING="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
if [ -n "$EXISTING" ]; then
  for pid in $EXISTING; do
    CMD="$(ps -o command= -p "$pid" 2>/dev/null | cut -c1-60)"
    case "$CMD" in
      *goose*) echo "stopping goose on port $PORT (PID $pid)"; kill "$pid" 2>/dev/null ;;
      *)       echo "port $PORT is held by something that is not goose (PID $pid): $CMD" >&2
               echo "refusing to kill it — pick another --port" >&2
               exit 1 ;;
    esac
  done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || break
    sleep 0.5
  done
fi

# Worker-thread stacks are 8MB (crates/goose-cli/src/main.rs). If you still hit
# "thread 'tokio-rt-worker' has overflowed its stack", this raises every thread
# that has no explicit size — tokio's workers included.
if [ "$BIG_STACK" -eq 1 ]; then
  export RUST_MIN_STACK=33554432
  echo "RUST_MIN_STACK=32MB"
fi

ARGS=(serve --dangerously-unauthenticated --host "$HOST" --port "$PORT")

echo "starting: goose ${ARGS[*]}"
echo "  ACP endpoint: ws://$HOST:$PORT/acp"

if [ "$BACKGROUND" -eq 1 ]; then
  LOG="${TMPDIR:-/tmp}/goose-serve-$PORT.log"
  nohup "$BINARY" "${ARGS[@]}" >"$LOG" 2>&1 &
  PID=$!
  sleep 4
  if kill -0 "$PID" 2>/dev/null && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "  running in background, PID $PID"
    echo "  log: $LOG"
    echo "  stop: kill $PID"
  else
    echo "failed to start — last lines of $LOG:" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
else
  exec "$BINARY" "${ARGS[@]}"
fi
