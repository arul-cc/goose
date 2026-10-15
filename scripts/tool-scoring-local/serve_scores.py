"""Read-only HTTP view of the recorded tool-call scores, for checking from Postman or curl.

  GET /healthz
  GET /scores                       every tool call, oldest first
  GET /scores?session=<id prefix>   one session
  GET /scores?min_risk=0.5          only calls scored at or above this

Each item is one tool call: the score from before it ran plus its outcome afterwards
(`outcome` is absent while it is still running). Binds 127.0.0.1 only.
Usage: serve_scores.py SCORE_DIR PORT
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "tool-scoring", "scripts"))
import show_scores  # noqa: E402

SCORE_DIR, PORT = sys.argv[1], int(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, payload):
        raw = json.dumps(payload, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/healthz":
            return self.reply(200, {"ok": True, "dir": SCORE_DIR})
        if url.path != "/scores":
            return self.reply(404, {"error": "try /scores"})
        try:
            min_risk = float(query.get("min_risk", ["0"])[0])
        except ValueError:
            return self.reply(400, {"error": "min_risk must be a number"})
        pairs = show_scores.read_records(SCORE_DIR, query.get("session", [""])[0], {})
        calls = [c for c in show_scores.join(pairs) if (c.get("risk") or 0) >= min_risk]
        self.reply(200, calls)


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
