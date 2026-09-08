import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence request logging
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        want_tools = "tools" in body
        # first request with tools -> answer with a tool call; the follow-up
        # (conversation containing a tool role) -> plain streamed answer
        has_tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        if want_tools and not has_tool_result:
            tool_call = {"function": {"name": "copy_text",
                                      "arguments": {"text": "stream-test"}},
                         "id": "call_1"}
            chunks = [
                {"message": {"role": "assistant", "content": "",
                             "tool_calls": [tool_call]}, "done": False},
                {"message": {"role": "assistant", "content": "Copied it."}, "done": True},
            ]
        else:
            chunks = [
                {"message": {"role": "assistant", "content": "One. "}, "done": False},
                {"message": {"role": "assistant", "content": "Two. "}, "done": False},
                {"message": {"role": "assistant", "content": "Three."}, "done": True},
            ]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for c in chunks:
            self.wfile.write((json.dumps(c) + "\n").encode("utf-8"))
            self.wfile.flush()


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
