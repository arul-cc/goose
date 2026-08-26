#!/usr/bin/env python3
"""Assert every ComplianceCow fork marker still exists after an upstream sync.

Static counterpart to validate-compliancecow.py: no services, no credentials,
runs in under a second. It catches the failure mode the test suites cannot —
a rebase silently deleting fork code. When a whole function or struct field
disappears, the tests that covered it disappear with it, so everything still
goes green while the feature is gone. That is exactly how the §4 injection side
was lost in the first sync.

    python3 scripts/fork-marker-sweep.py            # summary
    python3 scripts/fork-marker-sweep.py -v         # every marker

Exit 0 only if every marker holds. Markers are deliberately coarse — an
identifier and the file that must contain it. They prove code is still present,
not that it still works; validate-compliancecow.py and the Rust suite do that.

Adding a fork feature? Add its marker here in the same commit.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (section, marker, path, kind)
#   kind "present" — the marker must appear in the path
#   kind "absent"  — the marker must NOT appear (upstream behaviour we removed)
MARKERS = [
    # §1-3 per-session provider with an ephemeral tenant key
    ("§1-3", "UpdateSessionProviderRequest", "crates/goose-sdk-types/src/custom_requests.rs", "present"),
    ("§1-3", "_goose/unstable/session/provider/update", "crates/goose-sdk-types/src/custom_requests.rs", "present"),
    ("§1-3", "dispatch_update_session_provider", "crates/goose/src/acp/server/custom_dispatch.rs", "present"),
    ("§1-3", "on_update_session_provider", "crates/goose/src/acp/server.rs", "present"),

    # §4 header forwarding — injection side (store) and forwarding side (filter+send).
    # Losing either half leaves the other compiling, which is why both are listed.
    ("§4", "SetSessionExtensionDataRequest", "crates/goose-sdk-types/src/custom_requests.rs", "present"),
    ("§4", "_goose/unstable/session/extension_data/set", "crates/goose-sdk-types/src/custom_requests.rs", "present"),
    ("§4", "dispatch_set_session_extension_data", "crates/goose/src/acp/server/custom_dispatch.rs", "present"),
    ("§4", "filter_allowed_headers", "crates/goose/src/agents/extension_manager.rs", "present"),
    ("§4", "DynamicHeaderClient", "crates/goose/src/agents/extension_manager.rs", "present"),
    ("§4", "allowed_headers", "crates/goose/src/agents/extension.rs", "present"),
    ("§4", "allowed_headers", "crates/goose-sdk-types/src/custom_requests.rs", "present"),
    ("§4", "websocket_headers.v0", "crates/goose/src/agents/extension_manager.rs", "present"),

    # §6 Anthropic request metadata + DeepSeek cache_control incompatibility
    ("§6", "anthropic_user_metadata", "crates/goose/src/acp/server.rs", "present"),
    ("§6", "ANTHROPIC_DISABLE_CACHE", "crates/goose-provider-types/src/formats/anthropic.rs", "present"),

    # §9 DeepSeek thinking control
    ("§9", "GOOSE_THINKING_DISABLE_MODELS", "crates/goose-provider-types/src/formats/openai.rs", "present"),

    # §12 subagent multi-tenancy
    ("§12", "INHERITED_SUBAGENT_EXTENSION_STATES", "crates/goose/src/agents/platform_extensions/summon.rs", "present"),
    ("§12", "subagent_resume_enabled", "crates/goose/src/agents/platform_extensions/summon.rs", "present"),
    ("§12", "session_provider_credentials", "crates/goose/src/agents/platform_extensions/summon.rs", "present"),
    ("§12", "GOOSE_SUBAGENT_RESUME", "crates/goose/src/agents/platform_extensions/summon.rs", "present"),

    # Branding
    ("brand", "moocp", "crates/goose/src/prompts/system.md", "present"),
    ("brand", "moocp", "crates/goose/src/prompts/subagent_system.md", "present"),
    ("brand", "moocp", "crates/goose/src/prompts/tiny_model_system.md", "present"),

    # Operational tweaks found by identifier audit rather than by feature
    ("ops", "GOOSE_DB_MAX_CONNECTIONS", "crates/goose/src/session/session_manager.rs", "present"),

    # The fork's own gate must survive the rebase that would revert it
    ("gate", "recipe_session_is_named_from_its_conversation", "crates/goose/tests/compliancecow_features_test.rs", "present"),
    ("gate", "allow_listed_session_headers_are_forwarded", "crates/goose/tests/compliancecow_features_test.rs", "present"),
]

# Behaviour we deliberately removed. A plain file-wide grep would false-positive
# on unrelated recipe code, so these are scoped to one function body.
SCOPED_ABSENT = [
    (
        "§13",
        "crates/goose/src/session/session_manager.rs",
        r"pub async fn maybe_update_name",
        r"pub async fn search_chat_history",
        "recipe.title",
        "upstream's recipe short-circuit is back — every recipe session will be "
        "named after its recipe, collapsing the browser's session list to one title",
    ),
]

results = []


def record(section, name, ok, detail=""):
    results.append((section, name, ok, detail))
    return ok


def check_present(section, marker, rel, verbose):
    path = ROOT / rel
    if not path.exists():
        return record(section, f"{marker} in {rel}", False, "FILE MISSING — deleted or relocated upstream")
    ok = marker in path.read_text(errors="replace")
    if ok and verbose:
        record(section, f"{marker} in {rel}", True)
    elif ok:
        results.append((section, f"{marker} in {rel}", True, ""))
    else:
        record(section, f"{marker} in {rel}", False, "marker gone from this file")
    return ok


def check_scoped_absent(section, rel, start_re, end_re, marker, why):
    path = ROOT / rel
    if not path.exists():
        return record(section, f"{marker} absent in {rel}", False, "FILE MISSING")
    text = path.read_text(errors="replace")
    start = re.search(start_re, text)
    if not start:
        return record(section, f"{marker} absent in {rel}", False,
                      f"could not find {start_re!r} — the function was renamed or removed, "
                      "so this guard is no longer checking anything")
    end = re.search(end_re, text[start.end():])
    body = text[start.end(): start.end() + end.start()] if end else text[start.end():]
    if marker in body:
        return record(section, f"{marker} absent in {rel}", False, why)
    return record(section, f"{marker} absent in {rel}", True)


def relocated_hint(marker):
    """A marker gone from its file may have moved rather than vanished — say which."""
    try:
        out = subprocess.run(
            ["git", "grep", "-l", "--", marker],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        ).stdout.split()
    except Exception:
        return None
    return out[:3] or None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true", help="print every marker, not just failures")
    args = ap.parse_args()

    for section, marker, rel, kind in MARKERS:
        assert kind == "present", kind
        check_present(section, marker, rel, args.verbose)
    for section, rel, start_re, end_re, marker, why in SCOPED_ABSENT:
        check_scoped_absent(section, rel, start_re, end_re, marker, why)

    failures = [r for r in results if not r[2]]

    if args.verbose:
        for section, name, ok, detail in results:
            mark = "\033[32m ok \033[0m" if ok else "\033[31mFAIL\033[0m"
            print(f"  {mark} {section:6} {name}" + (f" — {detail}" if detail else ""))
    print(f"\n{len(results) - len(failures)}/{len(results)} fork markers hold")

    if not failures:
        print("No fork code went missing. This does NOT mean the features work — "
              "run scripts/validate-compliancecow.py for that.")
        return 0

    print(f"\n\033[31m{len(failures)} marker(s) failed:\033[0m")
    by_section = {}
    for section, name, _, detail in failures:
        by_section.setdefault(section, []).append((name, detail))
    for section in sorted(by_section):
        print(f"\n  {section}")
        for name, detail in by_section[section]:
            print(f"    {name}")
            print(f"      {detail}")
            # Only meaningful for a missing marker. For an absent-marker failure
            # the code being present elsewhere is the normal case, not a clue.
            if " absent in " not in name:
                hint = relocated_hint(name.split(" in ")[0])
                if hint:
                    print(f"      still present in: {', '.join(hint)} — likely relocated, not deleted")
    print("\nSee COMPLIANCECOW_FORK_FEATURES.md for what each section covers.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
