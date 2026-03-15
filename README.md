# FoxReach — Let AI Start the Conversation

FoxReach is a **B2B outbound sales / foreign trade** AI prototype that turns structured customer background data into actionable insights, a sales strategy, and stage-aware outreach emails—then closes the loop with **human approval**, **send/schedule**, and an **event-driven state machine**.

> Goal: move sales work from “research + write from scratch” to “capture structured context → AI drafts strategy + email → human approves → consistent follow-up cadence”.

---

## Background

In outbound sales, the real cost is rarely “sending”—it’s:
- Fragmented context: customer background lives across notes, LinkedIn, websites, spreadsheets
- High writing overhead: different stages (initial / follow-up / opened / replied) require different intent and tone
- Inconsistent follow-up: cadence depends on individuals, not a system
- Untrustworthy AI: generic outputs, or worse—hallucinated “facts”

FoxReach’s design principles:
- **Structured-first**: AI relies on persisted `CustomerBackground` in the database (auditable, reusable), not transient prompt text
- **Layered generation**: profile → strategy → stage-aware email drafts
- **Controlled loop**: AI creates drafts, but they must be `pending_approval` until a human reviews
- **State-driven**: events (sent/opened/replied) update `CustomerState`, which selects the next prompt path

---

## Workflow (End-to-End)

### 1) Customer & Background Intake (Sales Input)
1. Open Dashboard
2. Create a customer (name/email/company/industry, etc.)
3. Fill the **Customer Detail** form (full `CustomerBackground` schema)
4. Click **Save Background** to persist structured context in DB

### 2) AI Strategy Generation (Real LLM Output)
- After saving background, click **Write Outreach Email**
- The system loads `CustomerBackground` from DB and generates:
  - **Customer Profile (structured understanding)**
  - **Outreach Strategy (sales strategy)**
  - **Initial Draft Email (subject/body)**

> If the backend is missing `OPENAI_API_KEY`, strategy/follow-up generation returns **503** (no fake/mock output).

### 3) Human Approval
- Generated drafts are stored as `pending_approval`
- The Dashboard **Follow-up Board** lists all pending drafts
- Click **Review** to open the email review page:
  - edit subject/body
  - schedule send or send immediately

### 4) Send & Events (State Machine)
- Send Now / Schedule Send writes EmailEvent(`sent`)
- When `opened` / `replied` events happen (webhook or manual simulation):
  - EmailEvent is recorded
  - `CustomerState` advances accordingly

### 5) Stage-Aware Follow-ups
- When the customer is no longer `NEW_LEAD` (e.g., `CONTACTED`, `EMAIL_OPENED`, `FOLLOWUP_1`…)
- The system generates the next draft using a **stage-specific prompt** (still `pending_approval`)

---

## Architecture

### High-level
- **Frontend**: Next.js + Tailwind + shadcn UI (intake, approval, send)
- **Frontend API Proxy**: Next.js `/api/*` routes (same-origin proxy to backend, avoids CORS)
- **Backend**: FastAPI + SQLAlchemy (business logic, state machine, scheduler, LLM calls)
- **DB**: SQLite (self-contained demo/dev)
- **Scheduler**: APScheduler (daily scan to generate due follow-up drafts)

### Core data model
- `Customer`: identity (who we sell to)
- `CustomerBackground`: structured context (what we know)
- `CustomerState`: outreach state machine (where we are)
- `Email`: drafts + send records (`pending_approval`, `sent`, `opened`, `replied`)
- `EmailEvent`: event log (`sent`, `opened`, `replied`)
- `EmailSchedule`: scheduled send entries
- `EmailAccount`: sending identity/provider config

### State machine (simplified)
- `NEW_LEAD` → `CONTACTED` → `FOLLOWUP_1` → `FOLLOWUP_2` → `FOLLOWUP_3` → `STOPPED`
- Event-driven interrupts:
  - `EMAIL_OPENED`
  - `REPLIED` (stops automation)

---

## Features

### Frontend (Product UX)
- Demo login (localStorage session)
- Customer Dashboard 3-column layout:
  - Customer List: create/select/delete customers
  - Customer Detail: full structured background intake (aligned with backend schema)
  - Follow-up Board: lists `pending_approval` drafts and opens Review
- Email Review Page:
  - Customer Header: name/industry/status
  - AI Strategy Card: profile, purchasing behavior, price range, decision maker, recommended approach
  - Customer Insight Card: market position, target customers, product style, sustainability focus
  - Generated Email Card: editable subject/body
  - Send Now / Schedule Send

### Backend (Business + AI)
- Customer CRUD
- Background upsert/read (`CustomerBackground` as the AI input source)
- Strategy engine:
  - uses structured background + internal knowledge base + value blocks
  - returns structured profile + strategy + initial email draft
- Follow-up generator:
  - selects stage-aware prompts based on `CustomerState`
  - generates the next follow-up draft as `pending_approval`
- Email automation:
  - draft storage, edit, approval list
  - send now, schedule send
  - event recording and state updates
- Scheduler:
  - daily scan for due customers (1–3–7 cadence)
  - generates `pending_approval` follow-up drafts

### “Product-like” qualities (not a school demo)
- Explicit `pending_approval` workflow (AI never sends directly)
- State-driven prompts (different emails for different stages)
- Fail-fast AI: missing LLM config returns 503, never fake output
- Same-origin `/api/*` proxy for clean DX and safer config handling
- Structured context persistence enables iterative improvement and auditability

---

## Demo (Local Setup)

> Backend runs on **:8000**. Frontend runs on **:3000** and calls backend through `/api/*` proxy routes.

### Requirements
- Python 3.x
- Node.js LTS (for frontend)

### 1) Configure backend LLM (required for real AI)
Create/edit `.env` in repo root:

```env
OPENAI_API_KEY=YOUR_OPENAI_KEY
OPENAI_MODEL=gpt-4o
```

### 2) Start backend (FastAPI)
From repo root:

```bash
# Windows example (venv)
.\venv\Scripts\python.exe main.py
```

Verify:
- http://localhost:8000/system/status

### 3) Configure frontend proxy
Create/edit `foxreach-frontend/.env.local`:

```env
FOXREACH_BACKEND_URL=http://localhost:8000
```

> Restart `npm run dev` after changing `.env.local`.

### 4) Start frontend (Next.js)
```bash
cd foxreach-frontend
npm install
npm run dev
```

Open:
- http://localhost:3000/

### 5) Recommended demo script (3 minutes)
1. Homepage → **Try Demo** → Login (Use Demo / Login)
2. Dashboard → Create customer
3. Customer Detail → fill structured background → Save Background
4. Click **Write Outreach Email**
5. Review Page → edit → Send Now / Schedule Send
6. Follow-up Board shows pending approval drafts / updated status
7. (Optional) Simulate `opened` / `replied` via API and observe state changes

---

## API Overview (Backend)

> The frontend calls Next.js `/api/*` proxies. The backend endpoints are:

### Customers
- `POST /customers/`
- `GET /customers/` (includes state fields for UI badges)
- `GET /customers/{id}/background`
- `PUT /customers/{id}/background`

### Strategy / Value Content
- `POST /strategy/generate`
- `POST /value-content/generate`

### Emails
- `POST /emails/compose` (stores `pending_approval`)
- `GET /emails/pending-approval`
- `GET /emails/{id}`
- `PUT /emails/{id}` (persist edits)
- `POST /emails/send-now`
- `POST /emails/schedule`
- `POST /emails/events` (opened/replied)

### Follow-ups
- `GET /followups/state/{customer_id}`
- `POST /followups/generate-next`
- `POST /followups/generate-due` (scheduler)

---

## Roadmap
- Real auth (JWT / cookie session)
- Real email transport (SMTP/SendGrid/Gmail API) + observability
- Versioned strategy/profile persistence (Strategy table + AB testing)
- Configurable per-account cadence and timezone-aware scheduling
- Automated event ingestion (tracking pixel + inbound reply webhook)
- Feedback loop: reply outcomes → prompt/strategy tuning

---

## License
Prototype / internal use.
