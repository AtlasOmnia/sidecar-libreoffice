# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""libreoffice_undo and paragraph-mode writer_insert_text through the real plugin handlers.

A loopback fake bridge keeps a per-document undo stack, so every undo is checked by reading the
document back through the plugin's own read tools.
"""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest import mock

import plugin

TOKEN = "u" * 48
CALC = "lo:calc:1"
WRITER = "lo:writer:1"


class StatefulBridge:
    def __init__(self) -> None:
        self.cells = {"A1": "original"}
        self.paragraphs = ["Original text."]
        self.stacks = {CALC: [], WRITER: []}  # (title, snapshot)
        self.requests: list[dict] = []
        self.fail_undo_after: int | None = None
        self.uncertain_undo = False
        self.undos = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(payload)
                status, value = outer.handle(payload["op"], payload["args"])
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def snapshot(self, document_id):
        return copy.deepcopy(self.cells if document_id == CALC else self.paragraphs)

    def handle(self, op, args):
        document = {"document_id": args.get("document_id")}
        if op == "get_range":
            return 200, {"ok": True, "data": {"values": [[self.cells.get(args["address"], "")]]}}
        if op == "set_range":
            self.stacks[CALC].append((f"Hermes: set {args['address']}", self.snapshot(CALC)))
            self.cells[args["address"]] = args["values"][0][0]
            return 200, {"ok": True, "data": {"written": True}}
        if op == "writer_get_text":
            return 200, {"ok": True, "data": {"paragraphs": list(self.paragraphs)}}
        if op == "writer_insert_text":
            self.stacks[WRITER].append(("Hermes: insert Writer paragraph at end", self.snapshot(WRITER)))
            if args.get("as_paragraph"):
                self.paragraphs.append(args["text"])
            else:
                self.paragraphs[-1] += args["text"]
            return 200, {"ok": True, "data": {"inserted": True, "as_paragraph": args.get("as_paragraph", False)}}
        if op == "undo":
            if self.uncertain_undo:
                return 500, {"ok": False, "category": "ExecutionUncertain", "state": "started", "error": "unknown"}
            if self.fail_undo_after is not None and self.undos >= self.fail_undo_after:
                return 409, {"ok": False, "category": "Validation", "state": "not_started",
                             "error": "The last undo step is not a Hermes change; nothing was undone"}
            stack = self.stacks[args["document_id"]]
            if not stack:
                return 404, {"ok": False, "category": "NotFound", "state": "not_started", "error": "No undo action is available"}
            title, snapshot = stack.pop()
            if args["document_id"] == CALC:
                self.cells = snapshot
            else:
                self.paragraphs = snapshot
            self.undos += 1
            return 200, {"ok": True, "data": {"undone": True, "title": title, "activated_document": False, "document": document}}
        return 400, {"ok": False, "category": "Validation", "error": "unsupported"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Context:
    def __init__(self, port):
        self.port = port
        self.tools = {}

    def get_config(self, key, default=None):
        return self.port if key == "bridge_port" else default

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs["handler"]


class PluginUndoTests(unittest.TestCase):
    def setUp(self):
        self.bridge = StatefulBridge()
        self.addCleanup(self.bridge.close)
        patcher = mock.patch.object(plugin, "_secret", return_value=TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = Context(self.bridge.server.server_address[1])
        plugin.register(ctx)
        self.tools = ctx.tools

    def call(self, name, **args):
        return json.loads(self.tools[name](args))

    def cell(self):
        return self.call("calc_get_range", document_id=CALC, sheet="Sheet1", address="A1")["values"][0][0]

    def undo_requests(self):
        return [item["args"] for item in self.bridge.requests if item["op"] == "undo"]

    def test_undo_one_step_restores_the_calc_cell_on_readback(self):
        self.call("calc_set_range", document_id=CALC, sheet="Sheet1", address="A1", values=[["changed"]])
        self.assertEqual(self.cell(), "changed")
        result = self.call("libreoffice_undo", document_id=CALC)
        self.assertEqual(result["undone"], 1)
        self.assertEqual([step["title"] for step in result["steps"]], ["Hermes: set A1"])
        self.assertEqual(self.cell(), "original")
        self.assertEqual(self.undo_requests(), [{"document_id": CALC, "hermes_only": True}])

    def test_undo_several_steps_in_order(self):
        for value in ("one", "two"):
            self.call("calc_set_range", document_id=CALC, sheet="Sheet1", address="A1", values=[[value]])
        result = self.call("libreoffice_undo", document_id=CALC, steps=2)
        self.assertEqual(result["undone"], 2)
        self.assertEqual(self.cell(), "original")

    def test_stops_at_the_first_refused_step_and_reports_what_was_undone(self):
        for value in ("one", "two"):
            self.call("calc_set_range", document_id=CALC, sheet="Sheet1", address="A1", values=[[value]])
        self.bridge.fail_undo_after = 1
        result = self.call("libreoffice_undo", document_id=CALC, steps=3)
        self.assertIs(result["ok"], False)
        self.assertEqual(result["error"]["category"], "Validation")
        self.assertEqual([step["title"] for step in result["undone"]], ["Hermes: set A1"])
        self.assertEqual(len(self.undo_requests()), 2)  # never retried, never continued past a refusal
        self.assertEqual(self.cell(), "one")

    def test_nothing_to_undo_is_not_found(self):
        result = self.call("libreoffice_undo", document_id=CALC)
        self.assertEqual(result["error"]["category"], "NotFound")
        self.assertNotIn("undone", result)

    def test_uncertain_undo_is_reported_and_not_retried(self):
        self.call("calc_set_range", document_id=CALC, sheet="Sheet1", address="A1", values=[["changed"]])
        self.bridge.uncertain_undo = True
        result = self.call("libreoffice_undo", document_id=CALC, steps=2)
        self.assertEqual(result["error"]["category"], "ExecutionUncertain")
        self.assertEqual(len(self.undo_requests()), 1)

    def test_invalid_arguments_send_nothing(self):
        for args in ({}, {"document_id": CALC, "steps": 0}, {"document_id": CALC, "steps": 11},
                     {"document_id": CALC, "steps": True}, {"document_id": CALC, "steps": "1"},
                     {"document_id": CALC, "extra": 1}, {"document_id": ""}):
            result = json.loads(self.tools["libreoffice_undo"](args))
            self.assertEqual(result["error"]["category"], "Validation", args)
        self.assertEqual(self.bridge.requests, [])

    def test_paragraph_append_then_undo_leaves_no_empty_paragraph(self):
        inserted = self.call("writer_insert_text", document_id=WRITER, text="Q4 summary.", position="end", as_paragraph=True)
        self.assertTrue(inserted["as_paragraph"])
        self.assertEqual(self.call("writer_get_text", document_id=WRITER)["paragraphs"], ["Original text.", "Q4 summary."])
        self.call("libreoffice_undo", document_id=WRITER)
        self.assertEqual(self.call("writer_get_text", document_id=WRITER)["paragraphs"], ["Original text."])

    def test_as_paragraph_must_be_boolean_and_is_forwarded_only_when_given(self):
        bad = self.call("writer_insert_text", document_id=WRITER, text="x", position="end", as_paragraph="yes")
        self.assertEqual(bad["error"]["category"], "Validation")
        self.call("writer_insert_text", document_id=WRITER, text="x", position="end")
        sent = [item["args"] for item in self.bridge.requests if item["op"] == "writer_insert_text"]
        self.assertEqual(sent, [{"document_id": WRITER, "text": "x", "position": "end"}])


if __name__ == "__main__":
    unittest.main()
