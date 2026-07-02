"""
db.py
Supabase (Postgres) data layer for the content-agent.

Mirrors the org-scoping pattern used in risk-engine/src/db.js: every
function takes organization_id and scopes queries/writes to it.

Schema: see schema.sql (content_sources, generated_content,
curated_references, content_audit_log). Run schema.sql once in Supabase
before using this service.
"""

import os
from datetime import datetime, timezone
from typing import Optional, Any

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# content_sources
# ---------------------------------------------------------------------------

def create_source(organization_id: str, course_id: Optional[str], source_type: str,
                   source_url: str, filename: Optional[str] = None) -> dict:
    res = supabase.table("content_sources").insert({
        "organization_id": organization_id,
        "course_id": course_id,
        "source_type": source_type,
        "source_url": source_url,
        "filename": filename,
        "status": "uploaded",
    }).execute()
    return res.data[0]


def update_source_status(source_id: str, status: str, error_message: Optional[str] = None) -> None:
    update = {"status": status}
    if status == "ready":
        update["processed_at"] = _now()
    if error_message:
        update["error_message"] = error_message
    supabase.table("content_sources").update(update).eq("id", source_id).execute()


def get_source(source_id: str, organization_id: str) -> Optional[dict]:
    res = (
        supabase.table("content_sources")
        .select("*")
        .eq("id", source_id)
        .eq("organization_id", organization_id)
        .maybe_single()
        .execute()
    )
    return res.data if res else None


# ---------------------------------------------------------------------------
# generated_content
# ---------------------------------------------------------------------------

def insert_generated_content(organization_id: str, source_id: str, content_type: str,
                              payload: dict, format: str = "na",
                              bloom_level: Optional[str] = None,
                              quality_score: Optional[float] = None,
                              version: int = 1,
                              status: str = "draft") -> dict:
    res = supabase.table("generated_content").insert({
        "organization_id": organization_id,
        "source_id": source_id,
        "content_type": content_type,
        "format": format,
        "bloom_level": bloom_level,
        "payload": payload,
        "quality_score": quality_score,
        "version": version,
        "status": status,
    }).execute()
    row = res.data[0]
    _log_audit(row["id"], "generated", "system",
               f"content_type={content_type} version={version}")
    return row


def get_next_version(source_id: str, content_type: str) -> int:
    """Content is versioned per (source, content_type) pair - regenerating a
    quiz for a source creates version 2, 3, etc. rather than overwriting."""
    res = (
        supabase.table("generated_content")
        .select("version")
        .eq("source_id", source_id)
        .eq("content_type", content_type)
        .order("version", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]["version"] + 1
    return 1


def get_content_for_source(source_id: str, organization_id: str) -> list[dict]:
    res = (
        supabase.table("generated_content")
        .select("*")
        .eq("source_id", source_id)
        .eq("organization_id", organization_id)
        .order("content_type")
        .order("version", desc=True)
        .execute()
    )
    return res.data or []


def get_review_queue(organization_id: str) -> list[dict]:
    """All content awaiting instructor review, per PRD's
    'instructor review & approval workflow' requirement."""
    res = (
        supabase.table("generated_content")
        .select("*, content_sources(filename, source_type)")
        .eq("organization_id", organization_id)
        .eq("status", "pending_review")
        .order("created_at", desc=True)
        .execute()
    )
    return res.data or []


def set_review_decision(content_id: str, organization_id: str, decision: str,
                         reviewer_id: str, notes: Optional[str] = None) -> dict:
    res = (
        supabase.table("generated_content")
        .update({
            "status": decision,
            "reviewed_by": reviewer_id,
            "reviewed_at": _now(),
        })
        .eq("id", content_id)
        .eq("organization_id", organization_id)
        .select("*")
        .single()
        .execute()
    )
    _log_audit(content_id, decision, reviewer_id, notes)
    return res.data


def mark_pending_review(content_id: str) -> None:
    supabase.table("generated_content").update({"status": "pending_review"}).eq("id", content_id).execute()


# ---------------------------------------------------------------------------
# curated_references
# ---------------------------------------------------------------------------

def insert_curated_references(organization_id: str, source_id: str, references: list[dict]) -> list[dict]:
    if not references:
        return []
    rows = [{
        "organization_id": organization_id,
        "source_id": source_id,
        "title": r.get("title", "Untitled"),
        "url": r.get("url"),
        "description": r.get("description"),
        "relevance_score": r.get("relevance_score"),
    } for r in references if r.get("url")]
    if not rows:
        return []
    res = supabase.table("curated_references").insert(rows).execute()
    return res.data or []


def get_curated_references(source_id: str, organization_id: str) -> list[dict]:
    res = (
        supabase.table("curated_references")
        .select("*")
        .eq("source_id", source_id)
        .eq("organization_id", organization_id)
        .order("relevance_score", desc=True)
        .execute()
    )
    return res.data or []


# ---------------------------------------------------------------------------
# content_audit_log
# ---------------------------------------------------------------------------

def _log_audit(content_id: str, action: str, actor: str, notes: Optional[str] = None) -> None:
    try:
        supabase.table("content_audit_log").insert({
            "content_id": content_id,
            "action": action,
            "actor": actor,
            "notes": notes,
        }).execute()
    except Exception as e:
        # Audit logging should never break the main pipeline
        print(f"[WARNING] Failed to write audit log for content {content_id}: {e}")


def get_audit_trail(content_id: str) -> list[dict]:
    res = (
        supabase.table("content_audit_log")
        .select("*")
        .eq("content_id", content_id)
        .order("created_at")
        .execute()
    )
    return res.data or []
