#!/usr/bin/env bash
# Build the goose binary for the ComplianceCow setup.
#
#   ./scripts/build-goose.sh                # debug
#   ./scripts/build-goose.sh --release
#   ./scripts/build-goose.sh --force        # build even if goose is running
#
# Wraps `cargo build` with the three things that have actually bitten us:
#
#   1. macOS Python SSL — v8-goose downloads a 32MB prebuilt librusty_v8 with
#      Python, which fails CERTIFICATE_VERIFY_FAILED against the system certs.
#      Left unset, goose-cli never compiles, and `cargo check --all-targets`
#      silently SKIPS it — so real compile errors in the fork's own code hid
#      behind a dependency failure until someone tried to run the binary.
#
#   2. Overwriting a live binary — macOS SIGKILLs a running process whose
#      executable is rewritten in place ("Code Signature Invalid"). Rebuilding
#      while `goose serve` is up kills it out from under you, with no message
#      from the build. This refuses unless you pass --force.
#
#   3. A build that "succeeded" without producing a newer binary — verified at
#      the end rather than trusting cargo's exit code alone.
#
# Related gotcha this cannot prevent: `cargo clippy -p goose-cli --bin goose`
# shares the target directory with `cargo build` and leaves a non-runnable
# artifact in place of the binary. Run clippy and the binary disappears; rerun
# this script afterwards. run-goose.sh reports the missing/stale binary rather
# than letting you wonder why your fix "did nothing".

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"

PROFILE=debug
CARGO_FLAGS=""   # only ever "--release"; a plain string avoids
                 # bash 3.2's empty-array expansion under `set -u`
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --release) PROFILE=release; CARGO_FLAGS="--release"; shift ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

BINARY="$ROOT/target/$PROFILE/goose"

# --- 2. don't kill a running goose -----------------------------------------
RUNNING="$(pgrep -f "target/$PROFILE/goose serve" 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
if [ -n "$RUNNING" ] && [ "$FORCE" -eq 0 ]; then
  echo "goose serve is running (PID ${RUNNING}) from $BINARY."
  echo
  echo "Rebuilding would rewrite that executable in place, and macOS SIGKILLs"
  echo "processes whose binary changes underneath them — it would die with no"
  echo "explanation. Stop it first:"
  echo
  echo "    kill $RUNNING"
  echo
  echo "or re-run with --force if you intend to take it down."
  exit 1
fi
[ -n "$RUNNING" ] && echo "warning: --force given; goose serve (PID ${RUNNING}) will be killed by this build"

# --- toolchain --------------------------------------------------------------
if [ -f "$ROOT/bin/activate-hermit" ]; then
  # shellcheck disable=SC1091
  source "$ROOT/bin/activate-hermit" >/dev/null 2>&1 || true
fi

# --- 1. the rusty_v8 / SSL problem ------------------------------------------
if [ -z "${SSL_CERT_FILE:-}" ]; then
  if CERTS="$(python3 -m certifi 2>/dev/null)" && [ -n "$CERTS" ]; then
    export SSL_CERT_FILE="$CERTS"
    echo "SSL_CERT_FILE -> certifi bundle (for v8-goose's librusty_v8 download)"
  else
    echo "warning: python3 certifi not found. If the build fails in v8-goose with"
    echo "         CERTIFICATE_VERIFY_FAILED, run: python3 -m pip install certifi"
  fi
fi

BEFORE=0
[ -f "$BINARY" ] && BEFORE="$(stat -f %m "$BINARY" 2>/dev/null || stat -c %Y "$BINARY" 2>/dev/null || echo 0)"

echo "building goose ($PROFILE)..."
BUILD_LOG="$(mktemp)"
trap 'rm -f "$BUILD_LOG"' EXIT

# shellcheck disable=SC2086
if ! cargo build $CARGO_FLAGS -p goose-cli --bin goose 2>&1 | tee "$BUILD_LOG"; then
  # A stale half-downloaded librusty_v8 survives a plain rebuild; clearing the
  # two crates forces the download to run again with SSL_CERT_FILE set.
  if grep -qiE "rusty_v8|CERTIFICATE_VERIFY_FAILED|could not find native static library" "$BUILD_LOG"; then
    echo
    echo "v8 download failed — clearing v8-goose/v8 and retrying once..."
    cargo clean -p v8-goose -p v8 2>/dev/null
    # shellcheck disable=SC2086
    if ! cargo build $CARGO_FLAGS -p goose-cli --bin goose 2>&1 | tee "$BUILD_LOG"; then
      echo "build failed after retry" >&2
      exit 1
    fi
  else
    echo "build failed" >&2
    exit 1
  fi
fi

# --- 3. did it actually produce a binary? -----------------------------------
if [ ! -x "$BINARY" ]; then
  echo "build reported success but $BINARY does not exist" >&2
  exit 1
fi
AFTER="$(stat -f %m "$BINARY" 2>/dev/null || stat -c %Y "$BINARY" 2>/dev/null || echo 0)"
if [ "$AFTER" = "$BEFORE" ]; then
  echo "note: binary unchanged (nothing to rebuild)"
fi

echo
echo "built: $BINARY"
"$BINARY" --version 2>/dev/null || true
echo
echo "run it with: ./scripts/run-goose.sh"
