#!/usr/bin/env bash
# Reclaim disk from the build tree, cheapest option first.
#
#   ./scripts/clean-goose.sh              # caches only — safe, no dependency rebuild
#   ./scripts/clean-goose.sh --ours       # + our own crates (third-party stays compiled)
#   ./scripts/clean-goose.sh --all        # everything, including the v8 download
#   ./scripts/clean-goose.sh --dry-run    # show what each tier would free
#
# A debug build of this workspace reaches ~46GB. Most of it is not the artifacts
# you need:
#
#   target/debug/incremental   ~12-21GB   pure compile-speed cache, always safe
#   target/debug/deps          ~29GB      compiled dependencies, expensive to rebuild
#   target/debug/build         ~2GB       build-script output, includes v8
#
# Default tier removes only the cache. `--all` also removes the prebuilt
# librusty_v8, whose re-download needs network and a working cert bundle — on
# macOS that fails with CERTIFICATE_VERIFY_FAILED unless SSL_CERT_FILE is set,
# which is why build-goose.sh sets it. Don't reach for --all casually.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
TARGET="$ROOT/target"

TIER=cache
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ours)    TIER=ours; shift ;;
    --all)     TIER=all; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ ! -d "$TARGET" ]; then
  echo "nothing to clean: no $TARGET"
  exit 0
fi

size_of() { du -sk "$1" 2>/dev/null | cut -f1 | tr -d ' '; }
human()   { awk -v k="${1:-0}" 'BEGIN{ split("KB MB GB TB",u," "); i=1;
             while (k>=1024 && i<4) { k/=1024; i++ } printf "%.1f%s", k, u[i] }'; }

BEFORE=$(size_of "$TARGET")
echo "target/ is currently $(human "$BEFORE")"
echo "(per-item figures below are estimates; the freed total at the end is measured)"
echo

# --- don't pull the binary out from under a running server --------------------
# macOS SIGKILLs a process whose executable is replaced, and cleaning removes it
# outright, so a running goose would die with no explanation.
RUNNING="$(pgrep -f "$TARGET/.*/goose serve" 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
if [ -n "$RUNNING" ] && [ "$TIER" != "cache" ] && [ "$DRY" -eq 0 ]; then
  echo "goose serve is running (PID ${RUNNING}) from this target directory." >&2
  echo "This tier deletes its binary, which would kill it silently. Stop it first:" >&2
  echo >&2
  echo "    kill $RUNNING" >&2
  exit 1
fi

# Sizes are estimates: du counts a hardlinked file under every path it appears
# in, and cargo hardlinks freely between deps/ and incremental/, so these can add
# up to more than the space actually reclaimed. The freed figure printed at the
# end is measured, not estimated — trust that one.
report() { # label, path
  local sz; sz=$(size_of "$2")
  [ -z "$sz" ] && sz=0
  printf '  %-34s ~%s\n' "$1" "$(human "$sz")"
}

case "$TIER" in
  cache)
    echo "tier: caches only (dependencies stay compiled; next build is slower, not longer to link)"
    report "target/debug/incremental" "$TARGET/debug/incremental"
    report "target/release/incremental" "$TARGET/release/incremental"
    report "target/flycheck0" "$TARGET/flycheck0"
    report "target/tmp" "$TARGET/tmp"
    if [ "$DRY" -eq 0 ]; then
      rm -rf "$TARGET/debug/incremental" "$TARGET/release/incremental" \
             "$TARGET/flycheck0" "$TARGET/tmp"
    fi
    ;;
  ours)
    echo "tier: caches + our own crates (third-party dependencies stay compiled)"
    report "target/debug/incremental" "$TARGET/debug/incremental"
    if [ "$DRY" -eq 0 ]; then
      rm -rf "$TARGET/debug/incremental" "$TARGET/release/incremental" \
             "$TARGET/flycheck0" "$TARGET/tmp"
      # shellcheck disable=SC1091
      [ -f "$ROOT/bin/activate-hermit" ] && source "$ROOT/bin/activate-hermit" >/dev/null 2>&1
      for crate in $(ls "$ROOT/crates" 2>/dev/null); do
        cargo clean -p "$crate" 2>/dev/null
      done
    else
      echo "  plus: cargo clean -p for each crate under crates/"
    fi
    ;;
  all)
    echo "tier: everything — the next build recompiles all dependencies AND"
    echo "      re-downloads librusty_v8 (needs network; set SSL_CERT_FILE, or just"
    echo "      use ./scripts/build-goose.sh which does)."
    report "whole target/" "$TARGET"
    if [ "$DRY" -eq 0 ]; then
      # shellcheck disable=SC1091
      [ -f "$ROOT/bin/activate-hermit" ] && source "$ROOT/bin/activate-hermit" >/dev/null 2>&1
      cargo clean 2>/dev/null || rm -rf "$TARGET"
    fi
    ;;
esac

echo
if [ "$DRY" -eq 1 ]; then
  echo "dry run — nothing removed."
  exit 0
fi

AFTER=$(size_of "$TARGET")
[ -z "$AFTER" ] && AFTER=0
FREED=$((BEFORE - AFTER))
echo "target/ is now $(human "$AFTER") — freed $(human "$FREED")"
[ "$TIER" = "cache" ] && echo "Dependencies are untouched, so the next build only recompiles what changed."
echo "Rebuild with: ./scripts/build-goose.sh"
