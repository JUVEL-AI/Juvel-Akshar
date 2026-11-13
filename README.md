# Juvel admissions calling (v0.1)

Lead arrives -> cleaned -> call parent within 5 min (calling hours) -> greeting -> transfer to counselor
(with whisper: student + class) -> result logged. Retries, backup number, fallback message, alerts.

Provider-agnostic: only `juvelcall/dialers/*` knows about a calling provider.

## Run
    pip install -r requirements.txt
    python -m pytest                                   # 24 tests
    python -m juvelcall simulate sample_data/sample_leads.csv
    python -m juvelcall serve                          # API on :8000

## Run and check (in order)
1. `pip install -r requirements.txt`
2. `python -m pytest` : all tests should pass
3. `python -m juvelcall simulate sample_data/sample_leads.csv` : full pipeline with a fake dialer
4. Fill in `config.yaml` (dialer.type: vapi, numbers, emails, secret)
5. `python -m juvelcall check` : lists everything still to fix, prints READY when done
6. `python -m juvelcall test-call +91YOURNUMBER ecr` : ONE real call to a phone you hold
7. `python -m juvelcall serve`, then point the lead source at POST /webhooks/lead

## Endpoints (header X-Webhook-Secret)
- POST /webhooks/lead        new registration (Meta/Bigin/Make/form)
- POST /webhooks/call-event  {call_ref, kind: ANSWERED|TRANSFERRED|TRANSFER_FAILED|NO_ANSWER|BUSY|FAILED}
- POST /webhooks/vapi         Vapi end-of-call-report (set as the assistant server URL)
- POST /admin/tick, GET /report, GET /health

## Still to plug in (see CONFIRM in config.yaml)
Vapi keys + number id, real numbers, calling hours, sheet column names, SMTP.
