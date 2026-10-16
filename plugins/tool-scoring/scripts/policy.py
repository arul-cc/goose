"""Policy rules for tool calls: decide whether a call should be blocked.

Used by score_step.py on PreToolUse. The mode comes from GOOSE_POLICY_MODE:

  shadow   (default) evaluate every call and record the decision, but never block.
  enforce  block a call whose decision is "block". A policy that cannot be loaded blocks
           everything, because a broken policy that silently allows is the worse failure.
  off      do not evaluate.

Enforcing is only as strong as the hook running at all. If the hook command cannot start
(uv missing, a bad interpreter), goose applies the hook's `on_failure` from hooks/hooks.json.
The shipped value is "allow", which suits shadow mode. When enforcing, set "on_failure":
"block" on the PreToolUse hook so a hook that cannot run stops calls instead of passing them.
Checked against goose 1.55.0: allow lets the call through, block refuses it.

The policy is a JSON file (GOOSE_POLICY_FILE, default policy.json next to the plugin):

  {"version": 1,
   "allow_tools": ["^todo$", "^cow-rules__"],        optional; a tool matching none is blocked
   "rules": [{"id": "...", "tool": "<regex>", "arg_pattern": "<regex>", "min_risk": 0.8,
              "reason": "..."}]}

A rule blocks a call when ALL of its conditions hold: `tool` matches the tool name,
`arg_pattern` matches the call's arguments (as JSON, unredacted), and `min_risk` is at or
below the score the scorer gave. A rule must have at least one condition. A call that
matches no rule is allowed. If the scorer failed there is no score, so `min_risk` rules do
not match: deterministic rules still apply, the score-based one does not.
"""
import json
import os
import re

MODES = ("off", "shadow", "enforce")
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PolicyError(Exception):
    pass


def current_mode():
    """(mode, problem). An unrecognised GOOSE_POLICY_MODE falls back to shadow, never to
    enforce, and says so, so a typo cannot silently turn blocking on or hide the problem."""
    raw = os.environ.get("GOOSE_POLICY_MODE", "shadow").strip().lower() or "shadow"
    if raw in MODES:
        return raw, None
    return "shadow", f"GOOSE_POLICY_MODE={raw!r} is not one of {', '.join(MODES)}; using shadow"


def policy_path():
    return os.environ.get("GOOSE_POLICY_FILE") or os.path.join(PLUGIN_ROOT, "policy.json")


def _regex(value, where):
    try:
        return re.compile(value)
    except (re.error, TypeError) as exc:
        raise PolicyError(f"{where}: invalid regex {value!r}: {exc}")


def load(path=None):
    path = path or policy_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise PolicyError(f"cannot read {path}: {exc}")
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PolicyError(f"{path}: expected an object with \"version\": 1")

    allow = data.get("allow_tools")
    if allow is not None:
        if not isinstance(allow, list):
            raise PolicyError("allow_tools must be a list of regexes")
        allow = [_regex(a, "allow_tools") for a in allow]

    rules, seen = [], set()
    for index, raw in enumerate(data.get("rules") or []):
        where = f"rules[{index}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise PolicyError(f"{where}: needs a string \"id\"")
        if raw["id"] in seen:
            raise PolicyError(f"{where}: duplicate id {raw['id']!r}")
        seen.add(raw["id"])
        if raw.get("action", "block") != "block":
            raise PolicyError(f"{where}: only \"block\" rules exist")
        rule = {"id": raw["id"], "reason": str(raw.get("reason") or raw["id"]), "tool": None, "arg_pattern": None, "min_risk": None}
        if raw.get("tool") is not None:
            rule["tool"] = _regex(raw["tool"], f"{where}.tool")
        if raw.get("arg_pattern") is not None:
            rule["arg_pattern"] = _regex(raw["arg_pattern"], f"{where}.arg_pattern")
        if raw.get("min_risk") is not None:
            risk = raw["min_risk"]
            if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not 0 <= risk <= 1:
                raise PolicyError(f"{where}.min_risk must be a number from 0 to 1")
            rule["min_risk"] = float(risk)
        if rule["tool"] is None and rule["arg_pattern"] is None and rule["min_risk"] is None:
            raise PolicyError(f"{where}: needs at least one of tool, arg_pattern, min_risk")
        rules.append(rule)
    return {"allow_tools": allow, "rules": rules}


def evaluate(policy, tool_name, tool_input, risk):
    """What the policy says about one call: {"decision", "rules", "reason"}."""
    tool_name = tool_name or ""
    text = json.dumps(tool_input, default=str, sort_keys=True) if tool_input is not None else ""
    hits = []
    for rule in policy["rules"]:
        if rule["tool"] is not None and not rule["tool"].search(tool_name):
            continue
        if rule["arg_pattern"] is not None and not rule["arg_pattern"].search(text):
            continue
        if rule["min_risk"] is not None and (risk is None or risk < rule["min_risk"]):
            continue
        hits.append((rule["id"], rule["reason"]))
    allow = policy["allow_tools"]
    if allow is not None and not any(pattern.search(tool_name) for pattern in allow):
        hits.append(("not-in-allowlist", "the tool is not on the allow list"))
    if not hits:
        return {"decision": "allow", "rules": [], "reason": ""}
    return {"decision": "block", "rules": [h[0] for h in hits], "reason": "; ".join(h[1] for h in hits)}


def unavailable(problem):
    """The decision when the policy cannot be used. Block, so enforce fails closed."""
    return {"decision": "block", "rules": ["policy-unavailable"], "reason": "the policy could not be loaded", "error": str(problem)[:200]}
