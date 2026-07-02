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
import re
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks, Request, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

import db
import graph
import ingestion
from models import (
    IngestRequest, IngestResponse, JobStatusResponse,
    ReviewDecision, BatchReviewRequest, SourceStatus,
)

load_dotenv()

API_KEY = os.environ.get("CONTENT_AGENT_API_KEY")

# Content Agent service for Enlight Lab. (Trigger Redeploy)
app = FastAPI(title="Enlight Lab - Content Generation & Curation Agent", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to the dashboard's origin before production launch
    allow_methods=["*"],
    allow_headers=["*"],
)

_jobs: dict[str, dict] = {}


def require_auth(authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    if not API_KEY:
        return
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    elif authorization:
        token = authorization.strip()

    if not token or token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ingest", response_model=IngestResponse, dependencies=[Depends(require_auth)])
async def ingest(
    request: Request,
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    organization_id: Optional[str] = Form(None),
    course_id: Optional[str] = Form(None),
    source_type: Optional[str] = Form(None),
    source_url: Optional[str] = Form(None),
    filename: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    quiz_count: Optional[int] = Form(5),
    flashcard_count: Optional[int] = Form(5),
    summary_count: Optional[int] = Form(1),
    exercise_count: Optional[int] = Form(2),
):
    content_type = request.headers.get("content-type", "")

    # Case A: Multipart form / PDF Upload
    if file is not None or "multipart/form-data" in content_type:
        if file is None:
            raise HTTPException(status_code=400, detail="No file uploaded")
        
        filename_str = file.filename or filename or title or "document.pdf"
        if not filename_str.lower().endswith(".pdf") and file.content_type != "application/pdf":
            raise HTTPException(status_code=400, detail="Invalid file type. Only PDF files are supported.")

        pdf_bytes = await file.read()
        if len(pdf_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File size exceeds maximum limit of 10MB.")

        org_id = organization_id or "00000000-0000-0000-0000-000000000001"
        display_title = title or filename or file.filename or "Uploaded PDF"
        requested_counts = {
            "quiz": quiz_count if quiz_count is not None else 5,
            "flashcard": flashcard_count if flashcard_count is not None else 5,
            "summary": summary_count if summary_count is not None else 1,
            "exercise": exercise_count if exercise_count is not None else 2,
        }

        try:
            extracted_text = ingestion.parse_pdf_bytes(pdf_bytes)
        except Exception as e:
            err_msg = str(e)
            source = db.create_source(
                organization_id=org_id,
                course_id=course_id,
                source_type="pdf",
                source_url="",
                filename=display_title,
            )
            source_id = source["id"]
            job_id = str(uuid.uuid4())
            db.update_source_status(source_id, "failed", err_msg)
            _jobs[job_id] = {
                "source_id": source_id,
                "status": "failed",
                "current_step": "ingest",
                "error": err_msg,
            }
            return IngestResponse(job_id=job_id, source_id=source_id, status=SourceStatus.failed)

        # Upload extracted text as a plain text resource in Supabase Storage
        clean_name = re.sub(r"[^a-zA-Z0-9]", "_", display_title)
        storage_path = f"raw-text-{clean_name}-{uuid.uuid4()}.txt"
        
        try:
            db.supabase.storage.from_("course-materials").upload(
                storage_path,
                extracted_text.encode("utf-8"),
                {"content-type": "text/plain; charset=utf-8"}
            )
            public_url_data = db.supabase.storage.from_("course-materials").get_public_url(storage_path)
            pdf_text_url = public_url_data
        except Exception as upload_err:
            print(f"[WARNING] Supabase storage upload failed: {upload_err}")
            pdf_text_url = f"https://montijgrdxlfocvoeaxt.supabase.co/storage/v1/object/public/course-materials/{storage_path}"

        source = db.create_source(
            organization_id=org_id,
            course_id=course_id,
            source_type="pdf",
            source_url=pdf_text_url,
            filename=display_title,
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
            _run_pipeline_task, job_id, source_id, org_id,
            course_id, "pdf", pdf_text_url, requested_counts
        )

        return IngestResponse(job_id=job_id, source_id=source_id, status=SourceStatus.processing)

    # Case B: JSON Request
    body = await request.json()
    req = IngestRequest(**body)
    requested_counts = {
        "quiz": req.quiz_count if req.quiz_count is not None else 5,
        "flashcard": req.flashcard_count if req.flashcard_count is not None else 5,
        "summary": req.summary_count if req.summary_count is not None else 1,
        "exercise": req.exercise_count if req.exercise_count is not None else 2,
    }

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
        req.course_id, req.source_type.value, req.source_url, requested_counts
    )

    return IngestResponse(job_id=job_id, source_id=source_id, status=SourceStatus.processing)


async def _run_pipeline_task(job_id: str, source_id: str, organization_id: str,
                              course_id: Optional[str], source_type: str, source_url: str,
                              requested_counts: Optional[dict] = None):
    db.update_source_status(source_id, "processing")
    _jobs[job_id] = {
        "source_id": source_id,
        "status": "processing",
        "current_step": "ingest",
        "error": None,
    }
    try:
        last_state = {}
        async for event in graph.stream_pipeline(
            job_id, source_id, organization_id, course_id, source_type, source_url, requested_counts
        ):
            step = event.get("step")
            last_state = event.get("state", {})
            _jobs[job_id] = {
                "source_id": source_id,
                "status": "processing",
                "current_step": step,
                "error": None,
            }

        _jobs[job_id] = {
            "source_id": source_id,
            "status": last_state.get("status", "ready"),
            "current_step": "finalize",
            "error": last_state.get("error"),
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


@app.get("/content/review-queue", dependencies=[Depends(require_auth)])
def review_queue(organization_id: str):
    """Per PRD 'instructor review & approval workflow' - everything
    generated content starts as pending_review and instructors must
    explicitly approve before it's usable."""
    return db.get_review_queue(organization_id)


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


@app.post("/content/batch-review", dependencies=[Depends(require_auth)])
def batch_review(organization_id: str, request_body: BatchReviewRequest):
    count = db.set_batch_review_decision(
        request_body.content_ids, organization_id, request_body.decision.value,
        request_body.reviewer_id, request_body.notes
    )
    return {"updated_count": count}


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
