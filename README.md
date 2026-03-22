# AI_Outreach_System (AI Outreach Emails + Follow-up Cadence Experiment)

I started it for a simple reason: in B2B outreach (especially export sales), the daily work is repetitive—collect customer info, think through what to say, write the first email, follow up on day 1/3/7, and keep track of whether the customer replied or engaged. It’s easy to lose consistency and waste time.

This repo is a runnable version of what I built while exploring how AI can fit into a controlled workflow: it stores customer background in a structured way, generates email drafts, requires human review before sending, supports send/schedule, and uses a state machine to drive follow-ups.

---

## What I’m trying to solve

- **Writing emails is slow**: every email starts from scratch.
- **Follow-ups break easily**: the 1/3/7 cadence is often managed manually and gets messy.
- **AI can “make things up”**: my biggest concern is not tone—it’s hallucination (details that sound plausible but are not real).

So I made a design choice early on:  
**the LLM is only allowed to use structured customer background stored in the database + internal company knowledge + value content blocks.**  
No temporary form inputs, and as little “free improvisation” as possible.

---

## Core idea (my view on “controlled AI”)

I treat the system as a loop:

1. **Turn information into structured data** (customer background, company capabilities, cases, etc.)
2. **Constrain the model with prompts** (use only provided fields; do not invent facts)
3. **Never send directly after generation**: store as a draft and let a human review
4. **Sending/scheduling is a separate step**: traceable and auditable
5. **Use a state machine to drive the follow-up cadence**, so the system can recommend the next action

The point is: even if the model is unstable, the workflow is still safe (at least it won’t “auto-send”).

---

## Features (current status)

### 1) Customer management & structured background
- Create/delete customers
- Edit contact name (used for greetings)
- Country/region selection with timezone mapping (used for scheduled sending)

### 2) Initial outreach generation (NEW_LEAD)
- Reads customer background + internal knowledge + value content
- Generates a strategy (profile/strategy) and the first email draft
- Stores the draft in DB as `pending_approval`

### 3) Follow-up generation (1/3/7 cadence)
- Chooses follow-up stage based on customer state
- Includes the previous 1–2 outreach emails to reduce repetition
- Uses different “angles” per stage, and logs the angle so repeated drafts don’t recycle the same approach

### 4) Draft review & sending
- Review page allows editing subject/body
- Send Now
- Schedule Send by customer local hour (wheel-style UI)

### 5) Sending content cleanup (real-world issues I ran into)
- I had problems like subject prefixes, incomplete subjects, and HTML/placeholder residue showing up in received emails
- The send pipeline now normalizes subject, cleans body, and ensures consistent plain/html MIME parts

---

## Architecture overview (how I split modules)

### Frontend (Next.js App Router)
- `Dashboard`: customer management, background form, draft generation entry, pending-approval board
- `Email Review`: view/edit drafts, send/schedule
- Browser calls only `/api/*` (same-origin proxy), and Next.js forwards to FastAPI (avoids CORS)

### Backend (FastAPI + SQLAlchemy)
- **Data models** (simplified):
  - `Customer`: identity (who)
  - `CustomerBackground`: structured context (what we know)
  - `Email`: drafts + sent records (`pending_approval`, `sent`, …)
  - `EmailEvent`: events like sent/opened/replied
  - `EmailSchedule`: scheduled sending tasks
  - `CustomerState`: outreach state machine (drives follow-ups)

- **Service layer** (by responsibility):
  - `StrategyEngineService`: initial strategy + first email draft
  - `FollowUpOrchestratorService`: follow-up draft orchestration
  - `EmailAutomationService`: compose drafts, send/schedule, record events, update state
  - `SMTPTransport`: SMTP sending + tracking + content cleanup

---

## Running locally (how I run it)

### Backend
```bash
# From project root
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements.txt

.\venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### Frontend
```bash
cd tradeoutreachai-frontend
npm install
npm run dev -- --port 3001
```

URLs:
- Frontend: http://localhost:3001
- Backend: http://localhost:8000 (status: `/system/status`)

---

## Hard parts & trade-offs (my own notes)

### 1) How much can this prevent hallucination?
I’m leaning on engineering constraints rather than “trusting the model”:
- Input: structured background only (reduces free invention)
- Output: draft is never auto-sent; human review is mandatory

Remaining risk:
- The model can still add plausible but unsupported “facts” (MOQ, certifications, lead time, etc.)
- My next step would be “fact referencing/verification”: require `facts_used` with source fields, and run a pre-send check.

### 2) Why store drafts in DB?
For me this is the key step to make AI usable in a real workflow:  
stored drafts = reviewable, editable, traceable, and measurable.

### 3) Why a state machine?
Follow-up is not “generate another email”—it’s “decide the next action.”  
A state machine helps ensure consistency: who to follow up, which step, when, and when to stop.

---

## Next steps (if I keep building)
- **More complete tracking**: reliable reply/bounce attribution (IMAP/Webhooks)
- **More controllable outputs**: source references, key-fact checks, risk warnings
- **Could be a personal productivity tool**: a Python CLI to run daily actions quickly (add/bg/draft/send/schedule)
- **Production readiness**: Postgres migration, backups, logging, rate limiting, compliance (stop outreach)

---

## Demonstration
Step1. Sales inputs structured customer background instead of raw text.
<img width="1213" height="1416" alt="客户背调数据填入" src="https://github.com/user-attachments/assets/a0f6e603-b1e2-451f-bd09-1e37562c9cf0" />

Step2. The system first generates a strategy before writing emails. Email is generated based on structured data and strategy.
<img width="147" height="68" alt="image" src="https://github.com/user-attachments/assets/e9f30746-8b55-4b01-8b02-9fb49fdb60f7" />

Step3. Human-in-the-loop ensures controllability before sending. Emails are scheduled based on customer timezone.
<img width="2476" height="1360" alt="邮件发送确认页面" src="https://github.com/user-attachments/assets/e90dee7b-5f39-47e3-b2b0-9562488f2fc9" />
<img width="583" height="399" alt="image" src="https://github.com/user-attachments/assets/9049c12c-cce0-48b8-a757-f45e4b88741b" />

Step4. System tracks events and updates customer state automatically.
<img width="607" height="662" alt="image" src="https://github.com/user-attachments/assets/636a756f-0af0-4837-b338-c4bdea9519f8" />

Step5. Follow-ups are automatically generated with different angles.
Still under developing...


