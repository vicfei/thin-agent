#!/usr/bin/env python3
"""mock_llm_server.py —— 离线 OpenAI 兼容假端点，仅供冒烟测试。不属于交付内核。"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ["MOCK_PORT"])
SCENARIO_PATH = os.environ["MOCK_SCENARIO"]
REQLOG = os.environ["MOCK_REQLOG"]
NO_TOOLS = os.environ.get("MOCK_NO_TOOLS") == "1"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        with open(REQLOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
        if NO_TOOLS and "tools" in body:
            self._send(400, {"error": "tools not supported by this endpoint"})
            return
        msgs = body.get("messages") or []
        last = (msgs[-1].get("content") or "") if msgs else ""
        if last.startswith("你是压缩器"):
            self._send(200, {"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "压缩摘要：此前完成了测试准备工作。"}}],
                "usage": {"total_tokens": 30}})
            return
        scenario = json.load(open(SCENARIO_PATH, encoding="utf-8"))
        if not scenario:
            self._send(200, {"choices": [{"index": 0, "message": {
                "role": "assistant", "content": "(mock 脚本已耗尽)"}}],
                "usage": {"total_tokens": 10}})
            return
        step = scenario.pop(0)
        json.dump(scenario, open(SCENARIO_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        if step.get("match") and step["match"] not in json.dumps(body, ensure_ascii=False):
            with open(REQLOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"MATCH_FAIL": step["match"]}, ensure_ascii=False) + "\n")
        self._send(200, {"choices": [{"index": 0, "message": step["reply"]}],
                         "usage": {"total_tokens": 50}})


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
