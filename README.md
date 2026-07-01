# Agent 03 — Content Generation & Curation Agent

Separate Python/FastAPI + LangGraph microservice, per PRD architecture
decision. Lives independently from `risk-engine` (Node/Express) — different
folder, different deploy, own Supabase tables in the same project.

## What this does (per PRD)

Ingests course materials (PDF, video transcript, URL) and auto-generates:
- Quizzes (MCQ + open-ended, Bloom's Taxonomy-tagged)
- Flashcards
- Summaries
- Practice exercises (including case studies)

Also curates external content (articles/videos/case studies) via Gemini's
web search tool, and enforces an **instructor review & approval workflow**
before any generated content goes live — nothing is auto-published.

Every regeneration creates a new **version** rather than overwriting, and
every state change (generated / approved / rejected) is written to an
**audit log** table.

## Pipeline shape (LangGraph)

```
ingest → extract → generate → quality_check ─┬─→ (retry generate, up to 2x)
                                              ├─→ (next content_type)
                                              └─→ curate → finalize
```

The retry loop is why this needed LangGraph rather than a straight Express
route chain — quality_check can route back to generate.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app — all HTTP endpoints |
| `graph.py` | LangGraph pipeline definition (the core logic) |
| `ai_client.py` | Gemini wrapper — generation, quality check, web-search curation. Has mock fallback if `GEMINI_API_KEY` is unset, same pattern as risk-engine's `ai-service.js` |
| `ingestion.py` | PDF / video-transcript / URL text extraction |
| `db.py` | Supabase data layer, org-scoped like `risk-engine/src/db.js` |
| `models.py` | Pydantic request/response/state models |
| `schema.sql` | New Supabase tables — run once |
| `requirements.txt` | Python deps |
| `.env.example` | Copy to `.env` and fill in |

**Note on the Gemini SDK:** this uses the current `google-genai` package,
not the older `google-generativeai` package. The old one is deprecated by
Google as of writing — if you ever see AI-generated code elsewhere using
`import google.generativeai as genai`, that's the deprecated SDK; don't
copy that pattern into new code.

## Setup (Windows / PowerShell)

### 1. Create the folder and copy these files in

```powershell
mkdir D:\Edtech\content-agent
cd D:\Edtech\content-agent
# copy all files from this delivery into this folder
```

### 2. Create a virtual environment (recommended — keeps this isolated from risk-engine's Node setup and any other Python you have)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script with an execution-policy error,
run this once (as your normal user, not admin):
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 3. Run the new Supabase tables

Open Supabase → SQL Editor → paste the full contents of `schema.sql` → Run.
This only adds new tables (`content_sources`, `generated_content`,
`curated_references`, `content_audit_log`) — it does not touch anything
from Agent 02/04/05.

### 4. Set up `.env`

```powershell
copy .env.example .env
notepad .env
```

Fill in:
- `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` — same values as risk-engine's `.env`
- `GEMINI_API_KEY` — same key as risk-engine, or a fresh one, your call
- `CONTENT_AGENT_API_KEY` — generate any random string:
  ```powershell
  powershell -Command "[guid]::NewGuid().ToString()"
  ```

### 5. Run it locally

```powershell
python main.py
```

You should see Uvicorn start on `http://localhost:8001`. FastAPI gives you
free interactive API docs — open `http://localhost:8001/docs` in a browser
to see and test every endpoint without writing any client code.

### 6. Test end-to-end WITHOUT a real Gemini key first

Leave `GEMINI_API_KEY` blank in `.env` for the first test run. Every AI call
falls back to clearly-labeled `[MOCK]` output, so you can verify the whole
pipeline — ingestion → extraction → generation → quality check → curation →
Supabase writes → review queue → approve/reject — end to end, without
burning API quota or worrying about prompt quality yet.

**Quick test with a public PDF URL** (via `/docs` UI or curl):

```powershell
curl -X POST http://localhost:8001/ingest `
  -H "Authorization: Bearer <your CONTENT_AGENT_API_KEY>" `
  -H "Content-Type: application/json" `
  -d '{\"organization_id\": \"00000000-0000-0000-0000-000000000001\", \"source_type\": \"url\", \"source_url\": \"https://en.wikipedia.org/wiki/Photosynthesis\"}'
```

This returns a `job_id`. Poll it:

```powershell
curl http://localhost:8001/jobs/<job_id> -H "Authorization: Bearer <your key>"
```

Once `status: ready`, check Supabase's `generated_content` table — you
should see rows with `[MOCK]` payloads and `status: pending_review`.

Then check the review queue:
```powershell
curl "http://localhost:8001/content/review-queue?organization_id=00000000-0000-0000-0000-000000000001" `
  -H "Authorization: Bearer <your key>"
```

### 7. Once mock flow is confirmed working, add the real Gemini key

Set `GEMINI_API_KEY` in `.env`, restart the server, and re-run the same
`/ingest` test. This time you'll get real generated quizzes/flashcards/
summaries/exercises and real web-search-curated references.

## Known gaps / next steps (be upfront about these)

- **Auth is a shared bearer token (`CONTENT_AGENT_API_KEY`)**, not real
  per-user JWT verification like risk-engine's `requireAuth`. Fine for
  MVP/instructor-only access; revisit once this is wired into the actual
  dashboard so instructor identity flows through to `reviewed_by`.
- **Jobs are stored in memory** (a plain Python dict). If the service
  restarts mid-pipeline, in-flight job status is lost (though the
  underlying `content_sources.status` in Supabase will correctly show
  `processing` stuck, which at least tells you it needs re-running). Fine
  for MVP; a Redis or Supabase-backed jobs table would be the fix at scale.
- **PDF/video-transcript ingestion expects an already-uploaded file URL**
  (e.g. a Supabase Storage signed URL), not raw file bytes in the request.
  You'll need a small upload step (browser → Supabase Storage → get URL →
  call `/ingest`) on the dashboard side — this service intentionally
  doesn't handle file uploads directly to keep request payloads small.
- **Regeneration is whole-source, not per-content-type** — hitting
  "regenerate" on one flashcard currently re-runs the full pipeline (all 4
  content types) for that source. Fine to start; can be split into
  per-content-type regeneration later if instructors want more granular
  control without wasting quota.

## Deploying to Railway

Same pattern as risk-engine: new Railway service, point it at this folder
(or a separate repo — your call), set the same env vars from `.env` in
Railway's Variables tab, and Railway auto-detects Python + FastAPI. Add a
`Procfile` or set the start command to:
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```
Do this only after local testing (mock AND real Gemini) is fully confirmed
working — same "don't deploy until confirmed" rule as everything else in
this project.
