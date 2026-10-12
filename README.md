# sriai-distribution-agent

neatHack CLI: one company URL in, one scored first-touch draft out. **Email is never sent.**

## Architecture

```
URL → Profile (site text) → ICP + Meta ad (LLM)
    → Lead (Crustdata Person Search)
    → Message (draft) → Approval (score + threshold / y/n)
    → Cost row + follow-up schedule (printed only)
```

Every LLM and Crustdata call is a Neatlogs span. Score, decision, and cost are recorded on the workflow span (and cost on the child spans). `--bad-key` swaps in an invalid Crustdata token so a failed run still traces.

### Entities

| Entity | Meaning |
| --- | --- |
| **Lead** | One person object from Crustdata. The raw JSON is passed to the LLM; the app does not assume field names. |
| **Profile** | Scraped company-page text plus `ideal_customer` `{role, industry, company_size}`. |
| **Message** | Warm first-touch email (`subject`/`body`) plus the suggested Meta ad `{headline, primary_text, audience}`. |
| **Approval** | `draft_auto_approved` if score ≥ `AUTO_APPROVE_THRESHOLD` (default **80**, in config / `.env`). Else `draft_queued_for_review` and a terminal y/n. Outbox only; no SMTP. |
| **Cost** | Crustdata credits (0.03 × results) + gpt-4o-mini token USD, appended to `costs.csv`. |

### Operations

1. Fetch page text.
2. `gpt-4o-mini` → ICP + one Meta ad (JSON).
3. `POST https://api.crustdata.com/person/search` (`x-api-version: 2025-11-01`, Bearer from `.env`). Pick one lead.
4. Draft email → LLM score 0–100 + one-line reason → auto-approve or queue.
5. Print follow-ups at 24h, 36h, 72h, next Monday 9:00 IST. Do not send.
6. Append cost per lead to `costs.csv`.

### Integrations

- **OpenAI** `gpt-4o-mini` (JSON mode), wrapped with `neatlogs.wrap`.
- **Crustdata** Person Search; key `CRUSTDATA_API_KEY`.
- **Neatlogs** `init` at start; `trace` spans around each LLM call (`kind=LLM`) and Crustdata (`kind=TOOL`); `flush`/`shutdown` on exit.

### Events

`page_fetched` → `icp_ad_generated` → `lead_found` | `crustdata_failed` → `draft_scored` → `draft_auto_approved` | `draft_queued_for_review` → `followup_scheduled` → `cost_logged`.

## Setup

Python **3.10–3.13**. Copy `.env.example` to `.env`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python agent.py https://getsriai.com
python agent.py https://getsriai.com --bad-key   # traced failure
```
