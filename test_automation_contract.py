"""Contract tests for automation_helpers.py.

Run: python -m unittest -v test_automation_contract.py
No live DeepSeek, SMTP or n8n service is needed: the backend is mocked and the
HTTP tests talk to a throw-away local server.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import requests

import automation_helpers as ah

GOOD = dict(recipient=" analyst@example.com ", companies=[" Apple ", "Microsoft"],
            question="  Compare FY2024 revenue  ", priority="Urgent")
EVIDENCE = {"answer": " Apple revenue was X. ", "documents": [{"id": "D1"}], "calculations": []}


class ValidateTests(unittest.TestCase):
    def test_normalizes(self):
        out = ah.validate_research_request(**GOOD)
        self.assertEqual(out, {"recipient": "analyst@example.com", "companies": ["Apple", "Microsoft"],
                               "question": "Compare FY2024 revenue", "priority": "urgent"})

    def test_normal_priority_any_case(self):
        self.assertEqual(ah.validate_research_request("a@b.co", ["A"], "q", " NORMAL ")["priority"], "normal")

    def test_invalid_inputs_raise(self):
        bad = [
            dict(GOOD, recipient="not-an-email"), dict(GOOD, recipient=""), dict(GOOD, recipient=None),
            dict(GOOD, companies=[]), dict(GOOD, companies=["  ", ""]), dict(GOOD, companies=None),
            dict(GOOD, question="   "), dict(GOOD, question=None),
            dict(GOOD, question="x" * 2001),
            dict(GOOD, priority="high"), dict(GOOD, priority=""), dict(GOOD, priority=None),
        ]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ah.validate_research_request(**kwargs)


class ExtractTests(unittest.TestCase):
    def test_returns_clean_answer(self):
        self.assertEqual(ah.extract_research_text(EVIDENCE), "Apple revenue was X.")

    def test_calculations_alone_count_as_evidence(self):
        self.assertTrue(ah.extract_research_text({"answer": "ok", "documents": [], "calculations": [{"id": "A1"}]}))

    def test_missing_answer_or_evidence_raises(self):
        for result in ({"answer": "", "documents": [1]}, {"answer": "  ", "documents": [1]},
                       {"documents": [1]}, {"answer": "x", "documents": [], "calculations": []},
                       {"answer": 5, "documents": [1]}, None, "text"):
            with self.subTest(result=result), self.assertRaises(ValueError):
                ah.extract_research_text(result)


class PayloadTests(unittest.TestCase):
    def test_exact_five_fields(self):
        validated = ah.validate_research_request(**GOOD)
        payload = ah.build_webhook_payload(validated, "Report text")
        self.assertEqual(payload, {"recipient": "analyst@example.com", "company": "Apple, Microsoft",
                                   "question": "Compare FY2024 revenue", "priority": "urgent",
                                   "report": "Report text"})
        self.assertEqual(set(payload), {"recipient", "company", "question", "priority", "report"})

    def test_blank_report_raises(self):
        validated = ah.validate_research_request(**GOOD)
        for report in ("", "   ", None):
            with self.subTest(report=report), self.assertRaises(ValueError):
                ah.build_webhook_payload(validated, report)


class FakeResponse:
    def __init__(self, status, body=None, json_error=False):
        self.status_code, self._body, self._json_error = status, body, json_error
        self.ok = 200 <= status < 400

    def json(self):
        if self._json_error:
            raise ValueError("no json")
        return self._body


class PostToN8nMockedTests(unittest.TestCase):
    URL = "http://localhost:5678/webhook/atlas-priority"

    def post(self, **kw):
        with mock.patch("automation_helpers.requests.post", **kw) as m:
            result = ah.post_to_n8n(self.URL, {"report": "SECRET-BODY"}, timeout=5)
        return result, m

    def test_success_and_timeout_passed(self):
        body = {"status": "success", "message": "Urgent research alert submitted.", "priority": "urgent"}
        result, m = self.post(return_value=FakeResponse(200, body))
        self.assertEqual(result, {"http_status": 200, "status": "success",
                                  "message": "Urgent research alert submitted.", "priority": "urgent"})
        self.assertEqual(m.call_args.kwargs["timeout"], 5)
        self.assertEqual(m.call_args.kwargs["json"], {"report": "SECRET-BODY"})

    def test_http_200_with_error_status_is_error(self):
        result, _ = self.post(return_value=FakeResponse(200, {"status": "error", "message": "Nope"}))
        self.assertEqual((result["status"], result["message"]), ("error", "Nope"))

    def test_http_400_and_502(self):
        for code, msg in ((400, "Invalid recipient"), (502, "Email sending failed.")):
            with self.subTest(code=code):
                result, _ = self.post(return_value=FakeResponse(code, {"status": "error", "message": msg}))
                self.assertEqual(result, {"http_status": code, "status": "error", "message": msg})

    def test_http_error_even_if_body_says_success(self):
        result, _ = self.post(return_value=FakeResponse(502, {"status": "success"}))
        self.assertEqual(result["status"], "error")

    def test_non_json_reply(self):
        for code in (200, 500):
            with self.subTest(code=code):
                result, _ = self.post(return_value=FakeResponse(code, json_error=True))
                self.assertEqual((result["http_status"], result["status"]), (code, "error"))

    def test_json_but_not_object(self):
        result, _ = self.post(return_value=FakeResponse(200, ["list"]))
        self.assertEqual(result["status"], "error")

    def test_connection_error_and_timeout(self):
        for exc, expect in ((requests.exceptions.ConnectionError("boom"), "connect"),
                            (requests.exceptions.Timeout("slow"), "within"),
                            (requests.exceptions.RequestException("x"), "failed")):
            with self.subTest(exc=type(exc).__name__):
                result, _ = self.post(side_effect=exc)
                self.assertEqual((result["http_status"], result["status"]), (0, "error"))
                self.assertIn(expect, result["message"])
                self.assertNotIn("SECRET-BODY", result["message"])


class _Handler(BaseHTTPRequestHandler):
    routes = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.last_body = json.loads(self.rfile.read(length) or b"{}")
        code, body = self.routes[self.path]
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class PostToN8nRealHttpTests(unittest.TestCase):
    """Real HTTP round trips against a local stub that mimics the n8n response contract."""

    @classmethod
    def setUpClass(cls):
        _Handler.routes = {
            "/normal": (200, {"status": "success", "message": "Normal research email submitted.", "priority": "normal"}),
            "/invalid": (400, {"status": "error", "message": "Invalid recipient, company, question, priority, or report."}),
            "/llm": (502, {"status": "error", "message": "Report generation failed."}),
            "/html": (200, b"<html>not json</html>"),
        }
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_contract_responses(self):
        r = ah.post_to_n8n(self.base + "/normal", {"a": 1}, timeout=5)
        self.assertEqual((r["http_status"], r["status"], r["priority"]), (200, "success", "normal"))
        self.assertEqual(self.server.last_body, {"a": 1})
        r = ah.post_to_n8n(self.base + "/invalid", {}, timeout=5)
        self.assertEqual((r["http_status"], r["status"]), (400, "error"))
        r = ah.post_to_n8n(self.base + "/llm", {}, timeout=5)
        self.assertEqual((r["http_status"], r["message"]), (502, "Report generation failed."))
        r = ah.post_to_n8n(self.base + "/html", {}, timeout=5)
        self.assertEqual(r["status"], "error")

    def test_server_stopped_gives_connection_error(self):
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        url = f"http://127.0.0.1:{server.server_port}/x"
        server.server_close()  # nothing listens any more (n8n stopped)
        r = ah.post_to_n8n(url, {}, timeout=5)
        self.assertEqual((r["http_status"], r["status"]), (0, "error"))
        self.assertTrue(ah.format_automation_status(r).startswith("Error:"))


class FormatStatusTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(ah.format_automation_status(
            {"http_status": 200, "status": "success", "message": "Urgent alert sent."}), "Success: Urgent alert sent.")

    def test_error_cases(self):
        self.assertEqual(ah.format_automation_status(
            {"http_status": 400, "status": "error", "message": "Invalid input."}), "Error: Invalid input.")
        self.assertTrue(ah.format_automation_status({"http_status": 200, "status": "error"}).startswith("Error:"))
        self.assertTrue(ah.format_automation_status({"http_status": 502, "status": "success"}).startswith("Error:"))
        self.assertTrue(ah.format_automation_status({}).startswith("Error:"))
        self.assertTrue(ah.format_automation_status(None).startswith("Error:"))


class ExecuteTests(unittest.TestCase):
    ARGS = dict(key="k", recipient="analyst@example.com", companies=["Apple"], question="Compare revenue",
                priority="normal", store_type="FAISS", rerank=True, hybrid=False,
                webhook_url="http://localhost:5678/webhook/atlas-priority")
    OK = {"http_status": 200, "status": "success", "message": "Normal research email submitted.", "priority": "normal"}

    def run_flow(self, backend=None, post=None, **overrides):
        backend = backend or mock.Mock(return_value=EVIDENCE)
        post = post or mock.Mock(return_value=self.OK)
        with mock.patch("automation_helpers.answer_question", backend), \
                mock.patch("automation_helpers.post_to_n8n", post):
            out = ah.execute_research_automation(**{**self.ARGS, **overrides})
        return out, backend, post

    def test_happy_path(self):
        out, backend, post = self.run_flow()
        self.assertEqual(out, "Success: Normal research email submitted.")
        backend.assert_called_once_with("k", "Compare revenue", ["Apple"], "Both", "FAISS", True, False)
        url, payload = post.call_args.args[:2]
        self.assertEqual(url, self.ARGS["webhook_url"])
        self.assertEqual(payload, {"recipient": "analyst@example.com", "company": "Apple",
                                   "question": "Compare revenue", "priority": "normal", "report": "Apple revenue was X."})

    def test_invalid_input_skips_rag_and_webhook(self):
        for overrides in (dict(recipient="bad"), dict(question="  "), dict(priority="high"), dict(companies=[])):
            with self.subTest(overrides=overrides):
                out, backend, post = self.run_flow(**overrides)
                self.assertTrue(out.startswith("Error:"))
                backend.assert_not_called()
                post.assert_not_called()

    def test_backend_value_error_is_shown_without_webhook(self):
        out, _, post = self.run_flow(backend=mock.Mock(side_effect=ValueError("Select 1–5 uploaded companies.")))
        self.assertEqual(out, "Error: Select 1–5 uploaded companies.")
        post.assert_not_called()

    def test_unexpected_backend_error_is_sanitized(self):
        out, _, post = self.run_flow(backend=mock.Mock(side_effect=RuntimeError("sk-SECRET-KEY leaked")))
        self.assertTrue(out.startswith("Error:"))
        self.assertNotIn("SECRET", out)
        post.assert_not_called()

    def test_no_evidence_blocks_email(self):
        out, _, post = self.run_flow(backend=mock.Mock(
            return_value={"answer": "unavailable", "documents": [], "calculations": []}))
        self.assertTrue(out.startswith("Error:"))
        post.assert_not_called()

    def test_n8n_errors_are_reported(self):
        for result in ({"http_status": 0, "status": "error", "message": "Could not connect to n8n."},
                       {"http_status": 400, "status": "error", "message": "Invalid recipient, company, question, priority, or report."},
                       {"http_status": 502, "status": "error", "message": "Email sending failed."}):
            with self.subTest(result=result):
                out, _, _ = self.run_flow(post=mock.Mock(return_value=result))
                self.assertEqual(out, "Error: " + result["message"])

    def test_connection_failure_end_to_end(self):
        """No mocks for HTTP: webhook URL points at a closed port."""
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        url = f"http://127.0.0.1:{server.server_port}/webhook/atlas-priority"
        server.server_close()
        with mock.patch("automation_helpers.answer_question", mock.Mock(return_value=EVIDENCE)):
            out = ah.execute_research_automation(**{**self.ARGS, "webhook_url": url})
        self.assertTrue(out.startswith("Error: Could not connect to n8n"))


if __name__ == "__main__":
    unittest.main()
