"""
main.py
Agent 03 - Content Generation & Curation Agent
FastAPI service. Separate from risk-engine (Node/Express) per PRD's
architecture decision to use Python/LangGraph for multi-step document
processing chains.

Endpoints:
  POST /ingest                    -> upload a source (PDF/transcript/URL), kicks off pipeline async
  GET  /jobs/{job_id}             -> poll pipeline status
  GET  /content/{source_id}       -> fetch generated content for a source
  POST /content/{content_id}/regenerate -> re-trigger generation for one content_type
  GET  /content/review-queue      -> instructor review queue (pending_review items)
  POST /content/{content_id}/approve
  POST /content/{content_id}/reject
  GET  /curated/{source_id}       -> external curated references for a source
  GET  /health

Auth: Bearer token via CONTENT_AGENT_API_KEY env var for MVP (instructors/
course designers are the primary callers per PRD stakeholders). TODO: swap
for real Supabase JWT verification to match risk-engine's requireAuth
pattern once the dashboard integration is built.
"""

import os
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

import db
import graph
from models import (
    IngestRequest, IngestResponse, JobStatusResponse,
    ReviewDecision, SourceStatus,
)

load_dotenv()

API_KEY = os.environ.get("CONTENT_AGENT_API_KEY")

app = FastAPI(title="Enlight Lab - Content Generation & Curation Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the dashboard's origin before production launch
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store for MVP. A pipeline run for a large PDF can take
# 30s-2min (multiple LLM calls across 4 content types + retries), so this
# is async with a poll endpoint rather than a blocking request.
# TODO: swap for Redis or a Supabase-backed jobs table if this needs to
# survive service restarts or scale beyond one instance.
_jobs: dict[str, dict] = {}


def require_auth(authorization: Optional[str] = Header(None)):
    if not API_KEY:
        # No key configured - allow through for local dev, but warn loudly.
        print("[WARNING] CONTENT_AGENT_API_KEY not set - endpoint is UNAUTHENTICATED. "
              "Set this before deploying to Railway.")
        return
    if not authorization or authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse, dependencies=[Depends(require_auth)])
async def ingest(req: IngestRequest, background_tasks: BackgroundTasks):
    source = db.create_source(
        organization_id=req.organization_id,
        course_id=req.course_id,
        source_type=req.source_type.value,
        source_url=req.source_url,
        filename=req.filename,
    )
    source_id = source["id"]
    job_id = str(uuid.uuid4())

    _jobs[job_id] = {
        "source_id": source_id,
        "status": SourceStatus.processing.value,
        "current_step": "queued",
        "error": None,
    }

    background_tasks.add_task(
        _run_pipeline_task, job_id, source_id, req.organization_id,
        req.course_id, req.source_type.value, req.source_url,
    )

    return IngestResponse(job_id=job_id, source_id=source_id, status=SourceStatus.processing)


async def _run_pipeline_task(job_id: str, source_id: str, organization_id: str,
                              course_id: Optional[str], source_type: str, source_url: str):
    db.update_source_status(source_id, "processing")
    try:
        final_state = await graph.run_pipeline(
            job_id, source_id, organization_id, course_id, source_type, source_url
        )
        _jobs[job_id] = {
            "source_id": source_id,
            "status": final_state.get("status", "failed"),
            "current_step": final_state.get("current_step"),
            "error": final_state.get("error"),
        }
    except Exception as e:
        print(f"[ERROR] Pipeline crashed for job {job_id}: {e}")
        db.update_source_status(source_id, "failed", str(e))
        _jobs[job_id] = {
            "source_id": source_id,
            "status": "failed",
            "current_step": "unknown",
            "error": str(e),
        }


@app.get("/jobs/{job_id}", response_model=JobStatusResponse, dependencies=[Depends(require_auth)])
def get_job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        job_id=job_id,
        source_id=job["source_id"],
        status=job["status"],
        current_step=job.get("current_step"),
        error=job.get("error"),
    )


@app.get("/content/{source_id}", dependencies=[Depends(require_auth)])
def get_content(source_id: str, organization_id: str):
    source = db.get_source(source_id, organization_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    items = db.get_content_for_source(source_id, organization_id)
    return {"source": source, "content": items}


@app.post("/content/{content_id}/regenerate", dependencies=[Depends(require_auth)])
async def regenerate_content(content_id: str, organization_id: str,
                              background_tasks: BackgroundTasks):
    """Re-runs the pipeline for a single source (all content types) - a
    lighter-weight per-content-type regeneration can be added later if
    instructors want more granular control."""
    # Look up the source this content item belongs to
    existing = (
        db.supabase.table("generated_content")
        .select("source_id")
        .eq("id", content_id)
        .eq("organization_id", organization_id)
        .maybe_single()
        .execute()
    )
    if not existing or not existing.data:
        raise HTTPException(status_code=404, detail="Content not found")

    source_id = existing.data["source_id"]
    source = db.get_source(source_id, organization_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"source_id": source_id, "status": "processing", "current_step": "queued", "error": None}

    background_tasks.add_task(
        _run_pipeline_task, job_id, source_id, organization_id,
        source.get("course_id"), source["source_type"], source["source_url"],
    )

    return {"job_id": job_id, "source_id": source_id, "status": "processing"}


@app.get("/content/review-queue", dependencies=[Depends(require_auth)])
def review_queue(organization_id: str):
    """Per PRD 'instructor review & approval workflow' - everything
    generated content starts as pending_review and instructors must
    explicitly approve before it's usable."""
    return db.get_review_queue(organization_id)


@app.post("/content/{content_id}/approve", dependencies=[Depends(require_auth)])
def approve_content(content_id: str, organization_id: str, decision: ReviewDecision):
    updated = db.set_review_decision(content_id, organization_id, "approved",
                                      decision.reviewer_id, decision.notes)
    if not updated:
        raise HTTPException(status_code=404, detail="Content not found")
    return updated


@app.post("/content/{content_id}/reject", dependencies=[Depends(require_auth)])
def reject_content(content_id: str, organization_id: str, decision: ReviewDecision):
    updated = db.set_review_decision(content_id, organization_id, "rejected",
                                      decision.reviewer_id, decision.notes)
    if not updated:
        raise HTTPException(status_code=404, detail="Content not found")
    return updated


@app.get("/content/{content_id}/audit-trail", dependencies=[Depends(require_auth)])
def audit_trail(content_id: str):
    return db.get_audit_trail(content_id)


@app.get("/curated/{source_id}", dependencies=[Depends(require_auth)])
def curated_references(source_id: str, organization_id: str):
    return db.get_curated_references(source_id, organization_id)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
