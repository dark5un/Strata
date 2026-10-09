"""serve/harness/test_engine.py — the engine adapters.

make_service_engine is tested against a fake Service that mimics the real
prepare/run contract (server.py:2997/3194): events as (kind, payload) tuples.
make_http_engine is tested against a stdlib HTTP server speaking OpenAI SSE.

    python -m unittest serve.harness.test_engine -v
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..frontend import Event, ToolCall
from .engine import EngineResult, make_http_engine, make_service_engine


class FakeService:
    """Mimics Service.prepare + Service.run for one scripted reply."""

    def __init__(self, script):
        self.script = script          # list of (kind, payload)
        self.seen_messages = None
        self.seen_tools = None

    def prepare(self, messages, tools, kwargs, max_new=None, force=None, req=None):
        self.seen_messages = messages
        self.seen_tools = tools
        return list(range(len(messages))), False, 1024

    def run(self, ids, thinking, tools, max_new, sampling, cancel, force=None):
        yield from self.script


class ServiceEngineTests(unittest.TestCase):
    def test_content_and_usage(self):
        svc = FakeService([
            ("event", Event("content", text="Hel")),
            ("event", Event("content", text="lo")),
            ("final", {"finish": "stop", "prompt_tokens": 10, "completion_tokens": 2, "reused": 4}),
        ])
        eng = make_service_engine(svc)
        r = eng([{"role": "user", "content": "hi"}], None)
        self.assertIsInstance(r, EngineResult)
        self.assertEqual(r.content, "Hello")
        self.assertEqual(r.usage["prompt_tokens"], 10)
        self.assertEqual(r.usage["cached_tokens"], 4)
        self.assertEqual(r.finish, "stop")

    def test_tool_calls_assembled(self):
        call = ToolCall(name="shell", arguments={"command": "ls"}, id="c1")
        svc = FakeService([
            ("event", Event("tool_start", call=call)),
            ("event", Event("tool_args", call=call, text='{"comm')),
            ("event", Event("tool_args", call=call, text='and": "ls"}')),
            ("event", Event("tool_call", call=call)),
            ("final", {"finish": "stop", "prompt_tokens": 5, "completion_tokens": 3}),
        ])
        r = make_service_engine(svc)([{"role": "user", "content": "list"}], [{"name": "shell"}])
        self.assertEqual(r.tool_calls, [{"id": "c1", "name": "shell", "arguments": {"command": "ls"}}])

    def test_streamed_call_without_final_keeps_raw(self):
        call = ToolCall(name="shell", arguments={}, id="c9")
        svc = FakeService([
            ("event", Event("tool_start", call=call)),
            ("event", Event("tool_args", call=call, text='{"command": "trunc')),
            ("final", {"finish": "length", "prompt_tokens": 5, "completion_tokens": 9}),
        ])
        r = make_service_engine(svc)([{"role": "user", "content": "x"}], None)
        self.assertEqual(len(r.tool_calls), 1)
        self.assertEqual(r.tool_calls[0]["arguments_raw"], '{"command": "trunc')
        self.assertEqual(r.finish, "length")

    def test_reasoning_collected_and_streamed(self):
        seen = []
        svc = FakeService([
            ("event", Event("reasoning", text="think ")),
            ("event", Event("reasoning", text="hard")),
            ("event", Event("content", text="answer")),
            ("final", {"finish": "stop", "prompt_tokens": 1, "completion_tokens": 2}),
        ])
        r = make_service_engine(svc)([{"role": "user", "content": "q"}], None,
                                     on_event=lambda e: seen.append(e["kind"]))
        self.assertEqual(r.reasoning, "think hard")
        self.assertEqual(r.content, "answer")
        self.assertEqual(seen, ["reasoning", "reasoning", "content"])

    def test_ping_ignored(self):
        svc = FakeService([
            ("ping", None),
            ("event", Event("content", text="ok")),
            ("final", {"finish": "stop", "prompt_tokens": 1, "completion_tokens": 1}),
        ])
        self.assertEqual(make_service_engine(svc)([{"role": "user", "content": "x"}], None).content, "ok")


class _SSEServer(ThreadingHTTPServer):
    # declared, not monkey-patched: pyright sees the real shape
    chunks: list[dict]
    received: dict


class _SSEHandler(BaseHTTPRequestHandler):
    @property
    def sse_server(self) -> _SSEServer:
        return self.server  # type: ignore[return-value]  # set by _SSEServer's constructor pairing

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.sse_server.received = body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in self.sse_server.chunks:
            self.wfile.write(b"data: " + json.dumps(c).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, format, *args):
        pass


class HttpEngineTests(unittest.TestCase):
    def setUp(self):
        self.srv = _SSEServer(("127.0.0.1", 0), _SSEHandler)
        self.srv.chunks = []
        self.srv.received = {}
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_stream_assembly(self):
        self.srv.chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": "He"}}]},
            {"choices": [{"delta": {"content": "llo"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1",
                                                    "function": {"name": "read_file", "arguments": '{"path":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
                                                    "function": {"arguments": '"/x"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                       "prompt_tokens_details": {"cached_tokens": 2}}},
        ]
        eng = make_http_engine(self.base, "m", api_key="k")
        r = eng([{"role": "user", "content": "hi"}], [{"name": "read_file"}])
        self.assertEqual(r.content, "Hello")
        self.assertEqual(r.tool_calls, [{"id": "t1", "name": "read_file", "arguments": {"path": "/x"}}])
        self.assertEqual(r.usage, {"prompt_tokens": 7, "completion_tokens": 3, "cached_tokens": 2})
        sent = self.srv.received
        self.assertEqual(sent["model"], "m")
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["tools"][0]["function"]["name"], "read_file")

    def test_reasoning_delta(self):
        self.srv.chunks = [
            {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]},
        ]
        r = make_http_engine(self.base, "m")([{"role": "user", "content": "q"}], None)
        self.assertEqual(r.reasoning, "hmm")


if __name__ == "__main__":
    unittest.main()
