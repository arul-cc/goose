#!/usr/bin/env python3
"""Validate the ComplianceCow fork features against a running stack.

Run this after every upstream sync. The Rust and Go test suites cover the units;
this covers the wiring between them, which is where syncs actually break things —
a feature can compile, pass its tests, and still not reach cow-mcp.

    export CCOW_SECURITY_CONTEXT='{"ID":"...","DomainID":"...","AuthToken":"..."}'
    python3 scripts/validate-compliancecow.py

Checks needing a piece of the stack that is not running are reported SKIP, not
PASS, so a partial run can never look like a clean one. Exit status is 0 only if
nothing FAILed.

Credential values are never printed — only their presence and length.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    import websockets
except ImportError:
    sys.exit("needs `websockets`: pip3 install websockets")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results = []


def record(section, name, status, detail=""):
    results.append((section, name, status, detail))
    colour = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m"}[status]
    print(f"  {colour}{status:4}\033[0m {section:6} {name}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------
# ACP plumbing
# --------------------------------------------------------------------------

class Acp:
    """Minimal ACP client: JSON-RPC 2.0 over the goose WebSocket."""

    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self.updates = []

    @classmethod
    async def connect(cls, base_url, secret=""):
        url = base_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/acp"
        headers = {"x-secret-key": secret} if secret else {}
        ws = await websockets.connect(url, additional_headers=headers, max_size=None)
        self = cls(ws)
        await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        return self

    async def call(self, method, params, timeout=300):
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')} {msg['error'].get('data')}")
                return msg.get("result")
            if msg.get("method") == "session/update":
                self.updates.append(msg["params"])

    async def close(self):
        await self.ws.close()


async def check_acp_layer(args, sec_ctx):
    """Session administration over ACP — the methods that replaced REST."""
    try:
        acp = await Acp.connect(args.goose_url, args.secret)
    except Exception as e:
        record("ACP", "connect to goose", FAIL, f"{type(e).__name__}: {e}")
        return None
    record("ACP", "connect + initialize", PASS, args.goose_url)

    try:
        page = await acp.call("session/list", {})
        n = len(page.get("sessions", []))
        record("ACP", "session/list", PASS, f"{n} sessions, cursor={'yes' if page.get('nextCursor') else 'no'}")
    except Exception as e:
        record("ACP", "session/list", FAIL, str(e))

    # Full admin round-trip on a throwaway session so nothing real is touched.
    sid = None
    try:
        sid = (await acp.call("session/new", {"cwd": args.cwd, "mcpServers": []}))["sessionId"]
        await acp.call("_goose/unstable/session/rename", {"sessionId": sid, "title": "validate-probe"})
        info = (await acp.call("_goose/unstable/session/info", {"sessionId": sid}))["session"]
        assert info["title"] == "validate-probe", info["title"]
        record("ACP", "session/rename + info", PASS)

        forked = (await acp.call("session/fork", {"sessionId": sid, "cwd": args.cwd, "mcpServers": []}))["sessionId"]
        assert forked and forked != sid
        record("ACP", "session/fork", PASS, f"-> {forked}")

        for victim in (sid, forked):
            await acp.call("session/delete", {"sessionId": victim})
        try:
            await acp.call("_goose/unstable/session/info", {"sessionId": sid})
            record("ACP", "session/delete", FAIL, "deleted session still readable")
        except RuntimeError:
            record("ACP", "session/delete", PASS)
        sid = None
    except Exception as e:
        record("ACP", "session admin round-trip", FAIL, str(e))

    # §4 at the ACP layer: plant headers, call a tool, expect real data.
    if not sec_ctx:
        record("§4", "header forwarding (ACP)", SKIP, "CCOW_SECURITY_CONTEXT unset")
        await acp.close()
        return None

    try:
        sid = (await acp.call("session/new", {"cwd": args.cwd, "mcpServers": []}))["sessionId"]
        await acp.call("_goose/unstable/session/extension_data/set", {
            "sessionId": sid,
            "extensionData": {"websocket_headers.v0": {
                "authorization": json.loads(sec_ctx).get("AuthToken", ""),
                "x-cow-security-context": sec_ctx,
            }},
        })
        stored = await acp.call("_goose/unstable/session/extensions/list", {"sessionId": sid})
        record("§4", "extension_data/set accepted", PASS, f"session {sid}")
    except Exception as e:
        record("§4", "extension_data/set", FAIL, str(e))
        await acp.close()
        return None

    await acp.close()
    return sid


# --------------------------------------------------------------------------
# Bridge (browser -> CowGooseService -> ACP -> goose -> cow-mcp)
# --------------------------------------------------------------------------

async def drive_bridge(args, headers, prompt):
    """One turn through the bridge. Returns (frame_counts, tool_events, text_len)."""
    counts, tools, text_len = {}, [], 0
    url = args.cgs_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/goose/ws"
    session_id = str(uuid.uuid4())
    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "message", "content": prompt,
            "session_id": session_id, "session_type": args.session_type,
        }))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=args.turn_timeout)
            except asyncio.TimeoutError:
                counts["__timeout__"] = 1
                break
            frame = json.loads(raw)
            t = frame.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
            if t == "response":
                text_len += len(frame.get("content") or "")
            elif t == "tool_response":
                body = json.dumps(frame.get("data"), default=str)
                unauthorized = any(m in body.lower() for m in ("401", "unauthor", "forbidden"))
                tools.append({"bytes": len(body), "unauthorized": unauthorized})
            elif t in ("complete", "error"):
                break
    return counts, tools, text_len, session_id


async def check_bridge(args, sec_ctx):
    if not sec_ctx:
        record("§4", "header forwarding (bridge)", SKIP, "CCOW_SECURITY_CONTEXT unset")
        return None
    try:
        urllib.request.urlopen(args.cgs_url + "/v1/goose/sessions", timeout=5)
    except urllib.error.HTTPError:
        pass  # any HTTP answer means it is listening
    except Exception:
        record("§4", "header forwarding (bridge)", SKIP, f"CowGooseService not reachable at {args.cgs_url}")
        return None

    # Only the security context — sending Authorization too forces a call to
    # cowauthservice, which is a separate failure domain.
    headers = {"X-Cow-Security-Context": sec_ctx}
    prompt = args.prompt

    try:
        counts, tools, text_len, cow_sid = await drive_bridge(args, headers, prompt)
    except Exception as e:
        record("§4", "bridge turn (with headers)", FAIL, f"{type(e).__name__}: {e}")
        return None

    if counts.get("__timeout__"):
        record("§4", "bridge turn (with headers)", FAIL, "timed out waiting for completion")
        return None

    authorized = [t for t in tools if not t["unauthorized"]]
    if not tools:
        record("§4", "tool call with headers", FAIL, f"model called no tools; frames={counts}")
    elif authorized:
        record("§4", "tool call with headers", PASS,
               f"{len(authorized)} authorized, max {max(t['bytes'] for t in authorized)} bytes")
    else:
        record("§4", "tool call with headers", FAIL, "every tool result looks unauthorized")

    # The polymorphic-content bug made tool results vanish silently: requests
    # arrived, results never did.
    reqs, resps = counts.get("tool_request", 0), counts.get("tool_response", 0)
    if reqs and resps >= reqs:
        record("bridge", "tool results reach the browser", PASS, f"{reqs} req / {resps} resp")
    elif reqs:
        record("bridge", "tool results reach the browser", FAIL,
               f"{reqs} tool_request but only {resps} tool_response — check SessionUpdate.Content decoding")
    else:
        record("bridge", "tool results reach the browser", SKIP, "no tool calls this turn")

    record("bridge", "streaming to browser", PASS if text_len else FAIL,
           f"{counts.get('response', 0)} chunks, {text_len} chars")

    # Negative control: same prompt, no headers, must NOT be authorized.
    try:
        _, neg_tools, _, _ = await drive_bridge(args, {}, prompt)
        if not neg_tools:
            record("§4", "negative control (no headers)", SKIP, "model called no tools")
        elif all(t["unauthorized"] for t in neg_tools):
            record("§4", "negative control (no headers)", PASS, "cow-mcp rejected every call")
        else:
            record("§4", "negative control (no headers)", FAIL,
                   "tools returned data WITHOUT tenant headers — forwarding is not what authorised the call")
    except Exception as e:
        record("§4", "negative control (no headers)", SKIP, str(e))

    return cow_sid


# --------------------------------------------------------------------------
# Persisted session state
# --------------------------------------------------------------------------

def check_persisted_state(args, sec_ctx):
    """§1-3, §6, §9, §12, §13 as goose actually stored them."""
    db = os.path.expanduser(args.sessions_db)
    if not os.path.exists(db):
        record("state", "goose sessions.db", SKIP, f"not found at {db}")
        return
    # WAL: a plain copy misses recent writes, so read the live file read-only.
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("""SELECT id, name, provider_name, model_config_json, extension_data, recipe_json
                         FROM sessions WHERE extension_data LIKE '%websocket_headers%'
                         ORDER BY created_at DESC LIMIT 1""").fetchone()
    if not row:
        record("state", "a tenant session to inspect", SKIP, "none carry websocket_headers.v0 yet")
        return
    sid, name, provider, model_json, ext_json, recipe_json = row
    model = json.loads(model_json) if model_json else {}
    ext = json.loads(ext_json) if ext_json else {}
    rp = model.get("request_params") or {}

    wh = ext.get("websocket_headers.v0") or {}
    if "x-cow-security-context" in wh:
        record("§4", "websocket_headers.v0 persisted", PASS,
               f"{sorted(wh)} (lengths {[len(str(v)) for v in wh.values()]})")
    else:
        record("§4", "websocket_headers.v0 persisted", FAIL, f"got {sorted(wh)}")

    record("§1-3", "per-session provider", PASS if provider else FAIL,
           f"provider={provider} model={model.get('model_name')}")

    uid = (rp.get("metadata") or {}).get("user_id")
    expected = json.loads(sec_ctx).get("ID") if sec_ctx else None
    if uid and (not expected or uid == expected):
        record("§6", "anthropic metadata.user_id", PASS, f"len={len(uid)}")
    elif uid:
        record("§6", "anthropic metadata.user_id", FAIL, "present but does not match the security context ID")
    else:
        record("§6", "anthropic metadata.user_id", SKIP, "absent (only set for anthropic-family providers)")

    thinking = {k: v for k, v in rp.items() if "think" in k.lower()}
    record("§9", "deepseek thinking control", PASS if thinking else SKIP, json.dumps(thinking) if thinking else "no thinking params")

    tenant = ext.get("cow_tenant.v0")
    if tenant and tenant.get("session_type"):
        ids = "populated" if tenant.get("domain_id") and tenant.get("user_id") else "EMPTY (OSC_GOOSE=true?)"
        record("§12", "cow_tenant.v0", PASS, f"session_type={tenant['session_type']}, domain/user {ids}")
    else:
        record("§12", "cow_tenant.v0", FAIL, f"got {tenant}")

    # §13: the regression that shipped once — every recipe session named after
    # the recipe, so the browser's list showed one title for everything.
    if recipe_json:
        recipe_title = (json.loads(recipe_json) or {}).get("title")
        if name and recipe_title and name == recipe_title:
            record("§13", "session named from conversation", FAIL,
                   f"name == recipe title ({name!r}); the recipe short-circuit is back in maybe_update_name")
        else:
            record("§13", "session named from conversation", PASS, f"{name!r} vs recipe {recipe_title!r}")
        record("recipe", "recipe attached to session", PASS, str(recipe_title))
    else:
        record("§13", "session named from conversation", SKIP, "newest tenant session has no recipe")


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--goose-url", default=os.environ.get("GOOSE_URL", "http://127.0.0.1:3000"))
    p.add_argument("--cgs-url", default=os.environ.get("CGS_URL", "http://127.0.0.1:8080"))
    p.add_argument("--secret", default=os.environ.get("GOOSE_SERVER_SECRET_KEY", ""))
    p.add_argument("--session-type", default=os.environ.get("E2E_SESSION_TYPE", "rules"))
    p.add_argument("--sessions-db", default=os.environ.get(
        "GOOSE_SESSIONS_DB", "~/.local/share/goose/sessions/sessions.db"))
    p.add_argument("--cwd", default=os.getcwd())
    p.add_argument("--turn-timeout", type=int, default=240)
    p.add_argument("--prompt", default=os.environ.get(
        "E2E_PROMPT",
        "Use the compliancecow tools to report how many rules exist. Do not modify anything."))
    p.add_argument("--skip-bridge", action="store_true", help="ACP and DB checks only")
    args = p.parse_args()

    sec_ctx = os.environ.get("CCOW_SECURITY_CONTEXT", "").strip().strip('"').strip("'")
    if sec_ctx:
        try:
            json.loads(sec_ctx)
        except json.JSONDecodeError:
            sys.exit("CCOW_SECURITY_CONTEXT must be the security-context JSON string")

    print(f"\ngoose={args.goose_url}  cowgooseservice={args.cgs_url}  "
          f"security_context={'set' if sec_ctx else 'MISSING'}\n")

    asyncio.run(check_acp_layer(args, sec_ctx))
    if not args.skip_bridge:
        asyncio.run(check_bridge(args, sec_ctx))
    else:
        record("bridge", "browser -> service -> goose", SKIP, "--skip-bridge")
    # The bridge turn is what writes the state below, so inspect afterwards.
    time.sleep(1)
    check_persisted_state(args, sec_ctx)

    failed = [r for r in results if r[2] == FAIL]
    skipped = [r for r in results if r[2] == SKIP]
    print(f"\n{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if skipped:
        print("  skipped checks are NOT passes — bring the missing piece up and re-run:")
        for s in skipped:
            print(f"    {s[0]} {s[1]}: {s[3]}")
    if failed:
        print("\nFAILURES:")
        for f in failed:
            print(f"    {f[0]} {f[1]}: {f[3]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
