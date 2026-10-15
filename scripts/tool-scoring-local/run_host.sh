#!/usr/bin/env bash
# Run goose on this machine with the tool-scoring hook, the way CowGooseService expects it.
#
#   ./scripts/tool-scoring-local/run_host.sh up [--jev]   install the plugin, start goose on :3000 and
#                                                         the scores view on :3912
#   ./scripts/tool-scoring-local/run_host.sh down         stop both and remove the plugin
#   ./scripts/tool-scoring-local/run_host.sh status
#
# goose runs unauthenticated on 127.0.0.1 (see scripts/run-goose.sh), so the bridge's secret
# header is ignored. Providers and keys still come per session from the bridge, which gets
# them from Vault; nothing is read from here except the TypeSafe key for --jev.
#
# What `up` changes outside the repo, all undone by `down`:
#   ~/.agents/plugins/tool-scoring           the hook
#   ~/.config/goose/config.yaml              goose adds a `plugins:` entry for it on first start
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1
ROOT="$PWD"
BINARY="$ROOT/target/release/goose"
PLUGIN_SRC="$ROOT/plugins/tool-scoring"
PLUGIN_DST="$HOME/.agents/plugins/tool-scoring"
STATE="$HOME/.cache/goose-scoring-local"
SCORES="$HOME/.local/state/goose/tool-scores"
PORT=3000
SCORES_PORT=3912
KEYFILE="$HOME/.config/goose-scoring/typesafe.env"
TOKEN_FILE="$STATE/score-token"          # shared with run_bridge.sh
BRIDGE_PORT=8080

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
stop_pid() { alive "$1" && kill "$(cat "$1")" 2>/dev/null; rm -f "$1"; }

stop_goose() {
  local pid
  for pid in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null); do
    case "$(ps -o command= -p "$pid" | cut -c1-60)" in
      *goose*) kill "$pid" ;;
      *) die "port $PORT is held by something that is not goose (PID $pid)" ;;
    esac
  done
  for _ in $(seq 1 20); do lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || return 0; sleep 0.5; done
  die "goose on :$PORT did not stop"
}

down() {
  stop_pid "$STATE/scores-host.pid"
  stop_goose
  rm -rf "$PLUGIN_DST"
  echo "stopped; plugin removed (the 'plugins:' entry goose added to ~/.config/goose/config.yaml is left; it is harmless)"
}

status() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && echo "goose :$PORT   running" || echo "goose :$PORT   not running"
  alive "$STATE/scores-host.pid" && echo "scores :$SCORES_PORT running" || echo "scores :$SCORES_PORT not running"
  [ -d "$PLUGIN_DST" ] && echo "plugin         installed" || echo "plugin         not installed"
}

up() {
  local jev=0
  for arg in "$@"; do
    case "$arg" in --jev) jev=1 ;; *) die "unknown option: $arg" ;; esac
  done
  [ -x "$BINARY" ] || die "no binary at $BINARY; build with ./scripts/build-goose.sh --release"
  [ "$BINARY" -nt "$PLUGIN_SRC/hooks/hooks.json" ] || echo "note: the binary is older than the plugin; rebuild if you changed goose code"
  command -v uv >/dev/null || die "uv is not on PATH; the hook runs through it"
  [ "$jev" = 0 ] || [ -f "$KEYFILE" ] || die "--jev needs $KEYFILE"

  mkdir -p "$STATE" "$HOME/.agents/plugins"
  stop_goose
  rm -rf "$PLUGIN_DST"
  cp -r "$PLUGIN_SRC" "$PLUGIN_DST"
  find "$PLUGIN_DST" -name __pycache__ -exec rm -rf {} + 2>/dev/null

  if [ "$jev" = 1 ]; then
    # read the key in this shell, not on a command line where `ps` would show it
    GOOSE_SCORER_API_KEY="$(python3 - "$KEYFILE" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if line.startswith("GOOSE_SCORER_API_KEY="):
        v = line.partition("=")[2].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        print(v)
PY
)"
    export GOOSE_SCORER_API_KEY GOOSE_SCORER=jev GOOSE_SCORER_TIMEOUT=5
    # the hook's Python (python.org build) has no certificate bundle of its own
    export SSL_CERT_FILE="${SSL_CERT_FILE:-/etc/ssl/cert.pem}"
  fi

  # Let the hook hand each score to the bridge, which forwards it to the browser. Only when the
  # token exists, i.e. run_bridge.sh has been run; without it the hook just records to disk.
  if [ -s "$TOKEN_FILE" ]; then
    export GOOSE_SCORE_WEBHOOK="http://127.0.0.1:$BRIDGE_PORT/internal/tool-scores"
    GOOSE_SCORE_WEBHOOK_TOKEN="$(cat "$TOKEN_FILE")"
    export GOOSE_SCORE_WEBHOOK_TOKEN
  fi

  nohup "$BINARY" serve --dangerously-unauthenticated --platform desktop --enable-scheduler \
    --host 127.0.0.1 --port "$PORT" >"$STATE/goose-host.log" 2>&1 &
  echo $! >"$STATE/goose-host.pid"
  unset GOOSE_SCORER_API_KEY GOOSE_SCORE_WEBHOOK_TOKEN

  stop_pid "$STATE/scores-host.pid"
  mkdir -p "$SCORES"
  nohup python3 "$ROOT/scripts/tool-scoring-local/serve_scores.py" "$SCORES" "$SCORES_PORT" >"$STATE/scores-host.log" 2>&1 &
  echo $! >"$STATE/scores-host.pid"

  for _ in $(seq 1 40); do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
    sleep 0.5
  done
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || { tail -15 "$STATE/goose-host.log"; die "goose did not come up"; }
  echo "up. scorer: $([ "$jev" = 1 ] && echo Jev || echo "rules (offline)")"
  echo "  goose   ws://127.0.0.1:$PORT/acp   (log: $STATE/goose-host.log)"
  echo "  scores  http://127.0.0.1:$SCORES_PORT/scores"
}

case "${1:-}" in
  up) shift; up "$@" ;;
  down) down ;;
  status) status ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
