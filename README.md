# Atlas Priority Research Automation

Event-driven extension of Northstar Systems' **Atlas** research assistant. An employee asks a custom
research question, picks **Normal** or **Urgent**, and Atlas sends the answer to an **n8n** webhook, which
validates it, formats it with an LLM, and emails it with a priority-specific subject.

`backend.py` (the RAG backend) and the Lesson 3 app (`app_n8n.py`) are **unchanged**.

## Architecture

```
Gradio (app_automation.py)
  -> automation_helpers.execute_research_automation()
       validate -> backend.answer_question(question, source="Both") -> 5-field JSON
       -> POST http://localhost:5678/webhook/atlas-priority
n8n:
  Webhook -> If (Validate Research Request) --false--> Respond 400
                 | true
             Basic LLM Chain (DeepSeek) --error--> Respond 502 "Report generation failed."
                 | Markdown (-> HTML)
             If (Is Urgent?) --true--> Send Urgent Email --ok--> Respond 200 (urgent)
                             --false-> Send Normal Email --ok--> Respond 200 (normal)
             email error outputs --------------------------> Respond 502 "Email sending failed."
```

JSON contract: `recipient, company, question, priority, report`.

| Scenario | HTTP | Body |
|---|---|---|
| Normal sent | 200 | `{"status":"success","message":"Normal research email submitted.","priority":"normal"}` |
| Urgent sent | 200 | `{"status":"success","message":"Urgent research alert submitted.","priority":"urgent"}` |
| Invalid payload | 400 | `{"status":"error","message":"Invalid recipient, company, question, priority, or report."}` |
| LLM failure | 502 | `{"status":"error","message":"Report generation failed."}` |
| SMTP failure | 502 | `{"status":"error","message":"Email sending failed."}` |

## Files

| File | Purpose |
|---|---|
| `app_automation.py` | Atlas UI (copy of `app_n8n.py`) + section "4. Priority Research Request" |
| `automation_helpers.py` | validate / extract / payload / POST / status / orchestration |
| `test_automation_contract.py` | 26 offline tests (mocked backend, local stub HTTP server) |
| `n8n_workflow/atlas_priority_workflow.json` | sanitized n8n export (no credentials) |
| `Skill_Builder_Lab_Event_Driven_Automation_Completed.ipynb` | completed notebook + reflections |
| `.env.example` | configuration placeholders |

> Copy the original `sample_data/` folder from the Atlas repository next to these files if you want to
> reproduce the demo (it was not part of the supplied package).

## Local setup

```bash
python3 -m venv .venv                      # Windows: py -3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .\.venv\Scripts\python.exe ...
cp .env.example .env                       # then put your own DEEPSEEK_API_KEY in .env
.venv/bin/python app_automation.py         # http://127.0.0.1:7860
```
Stop `app_n8n.py`/`app.py` first (same port 7860).

### n8n (no Docker)
1. `npx n8n` (or `n8n start`) and open http://localhost:5678.
2. *Workflows -> Import from file* -> `n8n_workflow/atlas_priority_workflow.json`.
3. Attach your own credentials: **DeepSeek** on *DeepSeek Chat Model*, **SMTP** on both Send Email nodes; set
   the *From Email* of both nodes to your SMTP sender address.
4. Test with the Test URL (`/webhook-test/atlas-priority`, click *Listen for test event* first), then
   **Publish/Activate**. Atlas uses the production URL `/webhook/atlas-priority`
   (override with `N8N_WEBHOOK_URL` in `.env`).
5. In Atlas: upload company files, select companies, enter a recipient (use your own inbox), type a question,
   choose priority, click **Send Research Request**.

Direct webhook test (invalid priority must give HTTP 400 and no LLM/email execution):
```bash
curl -i -X POST http://localhost:5678/webhook/atlas-priority -H 'Content-Type: application/json' \
  -d '{"recipient":"me@example.com","company":"Apple","question":"q","priority":"high","report":"text"}'
```

## Tests
```bash
python -m unittest -v test_automation_contract.py
```
Result on the delivered code: **26 run, 26 passed, 0 failed.**

## Test matrix (T1-T6)
| Test | Offline evidence | Live evidence |
|---|---|---|
| T1 normal | happy-path test | _add screenshot `screenshots/normal_request.png`_ |
| T2 urgent | payload/priority tests | _add `screenshots/urgent_request.png`_ |
| T3 bad email | unit test, no webhook call | _add screenshot_ |
| T4 bad priority direct | n8n If expressions simulated in Node | _add `screenshots/invalid_request.png`_ |
| T5 empty question | unit test, backend not called | _add screenshot_ |
| T6 n8n stopped | closed-port test -> `Error: Could not connect...` | _add screenshot_ |

The live columns must be filled from your own run before submission.

## What changed from Lesson 3
Custom question instead of `FIXED_RESEARCH_QUESTION`; Normal/Urgent priority; new webhook `atlas-priority`;
webhook-side validation (400); LLM and SMTP error outputs (502); priority routing with distinct subjects;
structured response contract; input validation and evidence check in Python.

## Limitations
- Webhook has no authentication - localhost coursework only; never expose it publicly.
- Evidence presence does not prove every statement in the answer is correct; review the output.
- A successful SMTP hand-off does not guarantee inbox delivery.
- Email regex is a basic format check, not deliverability.
- Credentials (DeepSeek, SMTP) live only in n8n/`.env`, which are git-ignored.
