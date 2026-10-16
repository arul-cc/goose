# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Show the tool-call scores that score_step.py recorded: one row per tool call.

  show_scores.py                     all sessions, newest last
  show_scores.py --session ID        one session (a prefix is enough)
  show_scores.py --follow            keep printing as new calls arrive
  show_scores.py --min-risk 0.5      only calls scored at or above this
  show_scores.py --blocked           only calls the policy blocked, or would block in shadow mode
  show_scores.py --json              the joined records as JSON lines
  show_scores.py --dir PATH          read a copy of the directory (default $GOOSE_SCORE_DIR,
                                     then ~/.local/state/goose/tool-scores)

In a pod:
  kubectl exec -it <pod> -- uv run --no-project --no-python-downloads --python 3.12 \\
      ~/.agents/plugins/tool-scoring/scripts/show_scores.py --follow
"""
import argparse
import glob
import json
import os
import sys
import time


def default_dir():
    return os.environ.get("GOOSE_SCORE_DIR") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "goose", "tool-scores"
    )


def read_records(directory, session_prefix, offsets):
    """New records since `offsets`, as (ts, record) pairs. Updates `offsets` in place."""
    out = []
    for path in glob.glob(os.path.join(directory, "*.jsonl")):
        if session_prefix and not os.path.basename(path).startswith(session_prefix):
            continue
        with open(path, "rb") as fh:
            fh.seek(offsets.get(path, 0))
            data = fh.read()
        complete = data[: data.rfind(b"\n") + 1]
        offsets[path] = offsets.get(path, 0) + len(complete)
        for line in complete.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            out.append((record.get("ts", 0), record))
    out.sort(key=lambda pair: pair[0])
    return out


def join(pairs):
    """One dict per (session, tool_call_id): the pre record plus the post outcome."""
    calls = {}
    for _, record in pairs:
        key = (record.get("session_id"), record.get("tool_call_id"))
        call = calls.setdefault(key, {"session_id": key[0], "tool_call_id": key[1]})
        if record.get("phase") == "pre":
            call.update({k: v for k, v in record.items() if k != "phase"})
        elif record.get("phase") == "post":
            call["outcome"] = record.get("outcome")
            call["finished_ts"] = record.get("ts")
    return list(calls.values())


def policy_hit(call):
    """The policy's verdict when it says block, else None. `enforced` tells blocked from would-block."""
    verdict = call.get("policy") or {}
    return verdict if verdict.get("decision") == "block" else None


def fmt_row(call):
    risk = call.get("risk")
    score = "  n/a" if risk is None else f"{risk:4.2f}"
    label = (call.get("label") or ("error" if call.get("error") else "?")).ljust(6)
    when = time.strftime("%H:%M:%S", time.localtime(call.get("ts", 0))) if call.get("ts") else "--:--:--"
    why = "; ".join(call.get("reasons") or []) or (call.get("error") or "")
    outcome = call.get("outcome") or "running"
    hit = policy_hit(call)
    if hit:
        flag = "BLOCKED" if hit.get("enforced") else "would block"
        why = f"[{flag}: {', '.join(hit.get('rules') or [])}] {why}"
        if hit.get("enforced") and not call.get("outcome"):
            outcome = "blocked"  # goose never runs a blocked call, so no outcome record follows
    return (
        f"{when}  {str(call.get('session_id'))[:14]:<14}  {str(call.get('tool') or '?')[:34]:<34}  "
        f"{score} {label} {outcome:<8}  {why[:90]}"
    )


HEADER = f"{'time':<8}  {'session':<14}  {'tool':<34}  {'risk':<4} {'label':<6} {'outcome':<8}  why"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=default_dir())
    parser.add_argument("--session", default="")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--min-risk", type=float, default=0.0)
    parser.add_argument("--blocked", action="store_true", help="only calls the policy blocked or would block")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.dir) and not args.follow:
        print(f"no scores yet: {args.dir} does not exist", file=sys.stderr)
        return 1

    offsets = {}
    pending = {}
    printed_header = False
    while True:
        pairs = read_records(args.dir, args.session, offsets)
        if args.follow:
            # a call prints when its pre record arrives; its outcome follows as it lands
            for call in join(pairs):
                key = (call["session_id"], call["tool_call_id"])
                if "ts" in call:
                    pending[key] = call
                    risk = call.get("risk")
                    if (risk is not None and risk < args.min_risk) or (args.blocked and not policy_hit(call)):
                        continue
                    if args.json:
                        print(json.dumps(call), flush=True)
                    else:
                        if not printed_header:
                            print(HEADER)
                            printed_header = True
                        print(fmt_row(call), flush=True)
                elif call.get("outcome") and not args.json:
                    known = pending.get(key, {})
                    if known.get("risk") is None or known["risk"] >= args.min_risk:
                        print(f"          {'':<14}  {'↳ ' + str(known.get('tool') or call['tool_call_id']):<34}  {'':<4} {'':<6} {call['outcome']}", flush=True)
            time.sleep(1)
            continue
        calls = [c for c in join(pairs) if (c.get("risk") or 0) >= args.min_risk and (not args.blocked or policy_hit(c))]
        if args.json:
            for call in calls:
                print(json.dumps(call))
        elif not calls:
            print("no matching tool calls recorded")
        else:
            print(HEADER)
            for call in calls:
                print(fmt_row(call))
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
