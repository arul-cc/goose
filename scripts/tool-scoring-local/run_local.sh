#!/usr/bin/env bash
# Run goose with the tool-scoring hook on this machine, for testing from Postman or curl.
#
#   ./scripts/tool-scoring-local/run_local.sh up            rules scorer (offline)
#   ./scripts/tool-scoring-local/run_local.sh up --jev      score with Jev; key from
#                                                           ~/.config/goose-scoring/typesafe.env
#   ./scripts/tool-scoring-local/run_local.sh up --real     use a real LLM instead of the stand-in;
#                                                           settings from ~/.config/goose-scoring/provider.env
#   ./scripts/tool-scoring-local/run_local.sh up --no-build reuse the last image
#   ./scripts/tool-scoring-local/run_local.sh status | down
#
# Starts three things, all bound to 127.0.0.1:
#   goose            ws://127.0.0.1:3911/acp   (the hook is baked in)
#   stand-in LLM     127.0.0.1:9911            (not with --real) does what your message says; see mock_llm.py
#   scores view      http://127.0.0.1:3912/scores
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1
ROOT="$PWD"
HERE="$ROOT/scripts/tool-scoring-local"
STATE="$HOME/.cache/goose-scoring-local"
CONTAINER="goose-scoring-local"
IMAGE="goose-acp:scoring-local"
GOOSE_PORT=3911
SCORES_PORT=3912
LLM_PORT=9911
SECRET="local-test-secret"
KEYFILE="$HOME/.config/goose-scoring/typesafe.env"
PROVIDERFILE="$HOME/.config/goose-scoring/provider.env"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
stop_pid() { alive "$1" && kill "$(cat "$1")" 2>/dev/null; rm -f "$1"; }

down() {
  stop_pid "$STATE/llm.pid"
  stop_pid "$STATE/scores.pid"
  docker rm -f "$CONTAINER" >/dev/null 2>&1
  echo "stopped"
}

status() {
  printf 'goose container: '; docker ps --filter "name=^$CONTAINER\$" --format '{{.Status}}' | grep . || echo "not running"
  alive "$STATE/llm.pid" && echo "stand-in LLM:    running" || echo "stand-in LLM:    not running"
  alive "$STATE/scores.pid" && echo "scores view:     running" || echo "scores view:     not running"
}

up() {
  local jev=0 build=1 real=0
  for arg in "$@"; do
    case "$arg" in
      --jev) jev=1 ;;
      --no-build) build=0 ;;
      --real) real=1 ;;
      *) die "unknown option: $arg" ;;
    esac
  done
  command -v docker >/dev/null || die "docker not found"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running"
  if [ "$real" = 1 ] && [ ! -f "$PROVIDERFILE" ]; then
    die "--real needs $PROVIDERFILE with lines like GOOSE_PROVIDER=anthropic, GOOSE_MODEL=<model> and ANTHROPIC_API_KEY=<key>"
  fi
  if [ "$jev" = 1 ] && [ ! -f "$KEYFILE" ]; then die "--jev needs $KEYFILE (a line GOOSE_SCORER_API_KEY=...)"; fi

  down >/dev/null
  mkdir -p "$STATE/scores"
  chmod 777 "$STATE/scores"
  rm -f "$STATE"/scores/*.jsonl

  if [ "$build" = 1 ]; then
    echo "building image (a few seconds when the compile is cached)..."
    PATH=/usr/bin:$PATH "$ROOT/scripts/build-image.sh" --with-tool-scoring --tag scoring-local >"$STATE/build.log" 2>&1 \
      || { tail -20 "$STATE/build.log"; die "build failed, full log: $STATE/build.log"; }
    docker tag goose-acp:scoring-local "$IMAGE" 2>/dev/null || true
  fi
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no image $IMAGE; run without --no-build"

  if [ "$real" = 0 ]; then
    nohup python3 "$HERE/mock_llm.py" "$LLM_PORT" >"$STATE/llm.log" 2>&1 &
    echo $! >"$STATE/llm.pid"
  fi

  local envfile="$STATE/container.env"
  umask 077
  {
    echo "GOOSE_SERVER__SECRET_KEY=$SECRET"
    echo "GOOSE_MODE=auto"
    if [ "$real" = 1 ]; then
      python3 - "$PROVIDERFILE" <<'PY'
import re, sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    line = re.sub(r"^export\s+", "", line)
    name, sep, value = line.partition("=")
    name, value = name.strip(), value.strip()
    if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        continue
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    print(f"{name}={value}")
PY
    else
      echo "GOOSE_PROVIDER=anthropic"
      echo "GOOSE_MODEL=claude-sonnet-4-0"
      echo "ANTHROPIC_API_KEY=stand-in-not-a-real-key"
      echo "ANTHROPIC_HOST=http://host.docker.internal:$LLM_PORT"
    fi
    echo "GOOSE_SCORE_DIR=/scores"
    if [ "$jev" = 1 ]; then
      python3 - "$KEYFILE" <<'PY'
import sys
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if line.startswith("GOOSE_SCORER_API_KEY="):
        v = line.partition("=")[2].strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        print("GOOSE_SCORER_API_KEY=" + v)
PY
      echo "GOOSE_SCORER=jev"
      echo "GOOSE_SCORER_TIMEOUT=5"
    fi
  } >"$envfile"
  docker run -d --name "$CONTAINER" -p "127.0.0.1:$GOOSE_PORT:3000" \
    -v "$STATE/scores:/scores" --env-file "$envfile" "$IMAGE" >/dev/null \
    || { rm -f "$envfile"; die "could not start the container"; }
  rm -f "$envfile"

  nohup python3 "$HERE/serve_scores.py" "$STATE/scores" "$SCORES_PORT" >"$STATE/scores.log" 2>&1 &
  echo $! >"$STATE/scores.pid"

  for _ in $(seq 1 40); do
    (exec 3<>"/dev/tcp/127.0.0.1/$GOOSE_PORT") 2>/dev/null && break
    sleep 0.5
  done
  (exec 3<>"/dev/tcp/127.0.0.1/$GOOSE_PORT") 2>/dev/null || { docker logs "$CONTAINER" 2>&1 | tail -10; die "goose did not come up"; }

  echo
  echo "up. scorer: $([ "$jev" = 1 ] && echo "Jev" || echo "rules (offline)"); LLM: $([ "$real" = 1 ] && echo "real" || echo "stand-in")"
  echo "  goose (WebSocket)   ws://127.0.0.1:$GOOSE_PORT/acp      header  X-Secret-Key: $SECRET"
  echo "  scores              http://127.0.0.1:$SCORES_PORT/scores"
  [ "$real" = 0 ] && echo "  stand-in LLM log    $STATE/llm.log"
  echo "stop with: $0 down"
}

case "${1:-}" in
  up) shift; up "$@" ;;
  down) down ;;
  status) status ;;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
