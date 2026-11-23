"""Helpers for the Atlas priority research request -> n8n automation.

Flow: validate input -> Atlas answer_question() -> JSON payload -> n8n webhook
-> readable UI status. backend.py is reused unchanged (no new RAG pipeline).
"""
import logging
import re

import requests

try:  # Reuse the existing Atlas RAG backend.
    from backend import answer_question
except Exception as _import_error:  # pragma: no cover - lets tests run without RAG packages
    _BACKEND_IMPORT_ERROR = _import_error

    def answer_question(*args, **kwargs):  # type: ignore[no-redef]
        raise RuntimeError(f"Atlas backend could not be imported: {_BACKEND_IMPORT_ERROR}")

logger = logging.getLogger("atlas.automation")

VALID_PRIORITIES = {"normal", "urgent"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")  # basic format only
MAX_QUESTION_CHARS = 2000  # same limit as backend.answer_question()


def validate_research_request(
    recipient: str,
    companies: list[str],
    question: str,
    priority: str,
) -> dict:
    """Return normalized input fields or raise ValueError."""
    recipient = (recipient or "").strip()
    question = (question or "").strip()
    priority = (priority or "").strip().lower()

    if not recipient or not EMAIL_PATTERN.match(recipient):
        raise ValueError("Enter a valid recipient email address.")

    if isinstance(companies, str):  # tolerate a single company name
        companies = [companies]
    cleaned = [str(c).strip() for c in (companies or []) if c is not None and str(c).strip()]
    if not cleaned:
        raise ValueError("Select at least one company.")

    if not question:
        raise ValueError("Enter a research question.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"Keep the research question within {MAX_QUESTION_CHARS:,} characters.")

    if priority not in VALID_PRIORITIES:
        raise ValueError("Priority must be 'normal' or 'urgent'.")

    return {"recipient": recipient, "companies": cleaned, "question": question, "priority": priority}


def extract_research_text(research_result: dict) -> str:
    """Return a non-empty Atlas answer supported by at least one evidence item."""
    if not isinstance(research_result, dict):
        raise ValueError("Atlas returned an unexpected result.")
    answer = research_result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Atlas could not generate a research answer.")
    if not research_result.get("documents") and not research_result.get("calculations"):
        raise ValueError("Atlas found no supporting evidence in the selected files, so no email was sent.")
    return answer.strip()


def build_webhook_payload(validated: dict, report: str) -> dict:
    """Return recipient, company, question, priority and report for n8n."""
    if not isinstance(report, str) or not report.strip():
        raise ValueError("The research report is empty.")
    return {
        "recipient": validated["recipient"],
        "company": ", ".join(validated["companies"]),
        "question": validated["question"],
        "priority": validated["priority"],
        "report": report.strip(),
    }


def _error(http_status: int, message: str) -> dict:
    return {"http_status": http_status, "status": "error", "message": message}


def post_to_n8n(
    webhook_url: str,
    payload: dict,
    timeout: int = 180,
) -> dict:
    """POST JSON and return a normalized HTTP/workflow result dict.

    Never raises for network/HTTP problems and never echoes the payload.
    """
    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
    except requests.exceptions.Timeout:
        return _error(0, f"n8n did not respond within {timeout} seconds.")
    except requests.exceptions.ConnectionError:
        return _error(0, "Could not connect to n8n. Is n8n running and the workflow published?")
    except requests.RequestException as exc:
        return _error(0, f"Request to n8n failed ({type(exc).__name__}).")

    http_status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None

    if not isinstance(body, dict):
        if not response.ok:
            return _error(http_status, f"n8n returned HTTP {http_status} without a valid JSON reply.")
        return _error(http_status, "n8n returned a reply that is not valid JSON.")

    message = str(body.get("message") or "").strip()
    wf_status = str(body.get("status") or "").strip().lower()

    if not response.ok:
        return _error(http_status, message or f"n8n returned HTTP {http_status}.")
    if wf_status != "success":  # HTTP 200 alone is not success
        return _error(http_status, message or "The n8n workflow did not report success.")

    result = {"http_status": http_status, "status": "success",
              "message": message or "Research request submitted."}
    if body.get("priority"):
        result["priority"] = str(body["priority"])
    return result


def format_automation_status(result: dict) -> str:
    """Return a safe user-facing Success: or Error: message."""
    result = result if isinstance(result, dict) else {}
    http_status = result.get("http_status")
    ok_http = isinstance(http_status, int) and 200 <= http_status < 300
    message = str(result.get("message") or "").strip()
    if ok_http and result.get("status") == "success":
        return "Success: " + (message or "Research request submitted.")
    return "Error: " + (message or "The research request could not be completed.")


def execute_research_automation(
    key,
    recipient: str,
    companies: list[str],
    question: str,
    priority: str,
    store_type: str,
    rerank: bool,
    hybrid: bool,
    webhook_url: str,
) -> str:
    """Validate -> Atlas answer_question -> payload -> webhook -> UI status."""
    try:
        validated = validate_research_request(recipient, companies, question, priority)
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        research = answer_question(
            key, validated["question"], validated["companies"], "Both",
            store_type, rerank, hybrid,
        )
        report = extract_research_text(research)
        payload = build_webhook_payload(validated, report)
    except ValueError as exc:  # expected: bad workspace, no evidence, empty answer...
        return f"Error: {exc}"
    except Exception:
        logger.exception("Atlas research generation failed")
        return "Error: Atlas could not generate the research. See the terminal log for details."

    result = post_to_n8n(webhook_url, payload)
    return format_automation_status(result)
