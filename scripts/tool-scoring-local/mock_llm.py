"""A stand-in for the LLM, for testing the tool-scoring hook without a provider key.

Speaks the Anthropic Messages API. It does not reason: it reads your latest message and, if the
message asks for a tool call, makes exactly that call. Anything else gets a plain reply.

  run: <shell command>          calls the shell tool with that command
  call <tool> <json arguments>  calls the first tool whose name contains <tool>, e.g.
                                call create_rule {"name": "x"}

After the tool finishes it answers "done". Usage: mock_llm.py PORT
"""
import itertools
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])
calls_made = itertools.count()


RUN = re.compile(r"(?m)^run:\s*(.+)$")
CALL = re.compile(r"(?m)^call\s+(\S+)\s+(\{.*\})$")


def user_texts(message):
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [b["text"] for b in content or [] if b.get("type") == "text"]


def instruction(body):
    """(index of the user message holding the latest instruction, tool name, arguments), or None.

    goose adds its own context blocks around the message, so the instruction is searched for
    in every user message rather than assumed to be the last one.
    """
    names = [t["name"] for t in body.get("tools", [])]
    messages = body.get("messages", [])
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") != "user":
            continue
        for text in reversed(user_texts(messages[index])):
            match = RUN.search(text)
            if match and "shell" in names:
                return index, "shell", {"command": match.group(1).strip()}
            match = CALL.search(text)
            if match:
                wanted = next((n for n in names if match.group(1) in n), None)
                if wanted:
                    try:
                        return index, wanted, json.loads(match.group(2))
                    except ValueError:
                        pass
    return None


def tool_results_after(body, index):
    return sum(
        1
        for message in body.get("messages", [])[index + 1:]
        if isinstance(message.get("content"), list)
        for b in message["content"]
        if b.get("type") == "tool_result"
    )


def last_user_text(body):
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "user" and user_texts(message):
            return user_texts(message)[-1]
    return ""


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if not self.path.endswith("/messages"):
            self.send_response(404)
            self.end_headers()
            return
        found = instruction(body)
        finished = found is not None and tool_results_after(body, found[0]) > 0
        wanted = (found[1], found[2]) if found and not finished else None
        print(f"request: {len(body.get('tools', []))} tools, instruction {'found' if found else 'not found'}"
              f"{', already done' if finished else ''}", flush=True)
        if wanted:
            name, args = wanted
            block = {"type": "tool_use", "id": f"toolu_mock_{next(calls_made)}", "name": name, "input": args}
            stop = "tool_use"
            print(f"-> tool call {name} {json.dumps(args)[:120]}", flush=True)
        else:
            reply = "done" if finished else (
                "I only follow instructions of the form `run: <command>` or `call <tool> <json>`.")
            block = {"type": "text", "text": reply}
            stop = "end_turn"
        message = {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model", "mock"),
                   "content": [block], "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 5}}
        if not body.get("stream"):
            raw = json.dumps(message).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        w = self.wfile.write
        w(sse("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}))
        if block["type"] == "tool_use":
            w(sse("content_block_start", {"type": "content_block_start", "index": 0,
                                          "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}))
            w(sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        else:
            w(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
            w(sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": block["text"]}}))
        w(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        w(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                "usage": {"output_tokens": 5}}))
        w(sse("message_stop", {"type": "message_stop"}))


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
