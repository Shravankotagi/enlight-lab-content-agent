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
# helper functions for the 8 new tables
# ---------------------------------------------------------------------------

def _to_legacy_format(row: dict, content_type: str, status: str) -> dict:
    item = {
        "id": row["id"],
        "organization_id": row["organization_id"],
        "source_id": row["content_source_id"],
        "content_type": content_type,
        "bloom_level": row.get("bloom_level"),
        "quality_score": row.get("quality_score"),
        "version": row.get("version", 1),
        "status": status,
        "created_at": row.get("created_at"),
        "reviewed_by": row.get("reviewed_by"),
        "reviewed_at": row.get("reviewed_at"),
    }
    
    if "content_sources" in row:
        item["content_sources"] = row["content_sources"]
        
    if content_type == "quiz":
        ans_type = row.get("answer_type", "open_ended")
        item["format"] = ans_type
        item["payload"] = {
            "question": row.get("question"),
            "options": row.get("options"),
            "correct_answer": row.get("correct_answer"),
            "answer": row.get("correct_answer")
        }
    elif content_type == "flashcard":
        item["format"] = "na"
        item["payload"] = {
            "front": row.get("front"),
            "back": row.get("back")
        }
    elif content_type == "summary":
        item["format"] = "na"
        item["payload"] = {
            "summary": row.get("summary"),
            "key_takeaways": row.get("key_takeaways")
        }
    elif content_type == "exercise":
        cs = row.get("case_study")
        is_case = cs is not None and isinstance(cs, dict) and cs.get("scenario")
        item["format"] = "case_study" if is_case else "open_ended"
        item["payload"] = {
            "title": row.get("title") or "Practice Exercise",
            "task": row.get("task"),
            "prompt": row.get("task"),
            "question": row.get("task")
        }
        if is_case:
            item["payload"]["scenario"] = cs.get("scenario")
            
    return item


def _get_table_names(content_type: str) -> tuple[str, str]:
    if content_type == "quiz":
        plural = "quizzes"
    elif content_type == "exercise":
        plural = "exercises"
    elif content_type == "summary":
        plural = "summaries"
    elif content_type == "flashcard":
        plural = "flashcards"
    else:
        raise ValueError(f"Unknown content type: {content_type}")
    return f"review_{plural}", f"approved_{plural}"


def _find_review_item(content_id: str, organization_id: str) -> tuple[Optional[str], Optional[dict]]:
    for c_type in ("quiz", "flashcard", "summary", "exercise"):
        review_table, _ = _get_table_names(c_type)
        res = supabase.table(review_table).select("*").eq("id", content_id).eq("organization_id", organization_id).maybe_single().execute()
        if res and res.data:
            return c_type, res.data
    return None, None


def get_content_source_id(content_id: str, organization_id: str) -> Optional[str]:
    for c_type in ("quiz", "flashcard", "summary", "exercise"):
        review_table, approved_table = _get_table_names(c_type)
        res = supabase.table(review_table).select("content_source_id").eq("id", content_id).eq("organization_id", organization_id).maybe_single().execute()
        if res and res.data:
            return res.data["content_source_id"]
            
    for c_type in ("quiz", "flashcard", "summary", "exercise"):
        review_table, approved_table = _get_table_names(c_type)
        res = supabase.table(approved_table).select("content_source_id").eq("id", content_id).eq("organization_id", organization_id).maybe_single().execute()
        if res and res.data:
            return res.data["content_source_id"]
            
    return None


# ---------------------------------------------------------------------------
# review & approved content operations
# ---------------------------------------------------------------------------

def insert_generated_content(organization_id: str, source_id: str, content_type: str,
                              payload: dict, format: str = "na",
                              bloom_level: Optional[str] = None,
                              quality_score: Optional[float] = None,
                              version: int = 1,
                              status: str = "draft") -> dict:
    review_table, _ = _get_table_names(content_type)
    if content_type == "quiz":
        data = {
            "question": payload.get("question", "Quiz Question"),
            "answer_type": format,
            "options": payload.get("options"),
            "correct_answer": payload.get("correct_answer") or payload.get("answer")
        }
    elif content_type == "flashcard":
        data = {
            "front": payload.get("front", "Flashcard Term"),
            "back": payload.get("back", "Flashcard Definition")
        }
    elif content_type == "summary":
        data = {
            "summary": payload.get("summary", "Summary Overview"),
            "key_takeaways": payload.get("key_takeaways")
        }
    elif content_type == "exercise":
        data = {
            "title": payload.get("title") or "Practice Exercise",
            "task": payload.get("prompt") or payload.get("task") or payload.get("question") or payload.get("scenario") or "Exercise Prompt",
            "case_study": {"scenario": payload.get("scenario")} if format == "case_study" or "scenario" in payload else None
        }
    else:
        raise ValueError(f"Unknown content type: {content_type}")

    common = {
        "organization_id": organization_id,
        "content_source_id": source_id,
        "course_id": None,
        "bloom_level": bloom_level,
        "quality_score": quality_score,
        "version": version
    }
    data.update(common)
    res = supabase.table(review_table).insert(data).execute()
    row = res.data[0]
    _log_audit(row["id"], "generated", "system", f"content_type={content_type} version={version}")
    return _to_legacy_format(row, content_type, "pending_review")


def get_next_version(source_id: str, content_type: str) -> int:
    review_table, approved_table = _get_table_names(content_type)
    
    v1 = 0
    res1 = supabase.table(review_table).select("version").eq("content_source_id", source_id).order("version", desc=True).limit(1).execute()
    if res1.data:
        v1 = res1.data[0]["version"]
        
    v2 = 0
    res2 = supabase.table(approved_table).select("version").eq("content_source_id", source_id).order("version", desc=True).limit(1).execute()
    if res2.data:
        v2 = res2.data[0]["version"]
        
    return max(v1, v2) + 1


def get_content_for_source(source_id: str, organization_id: str) -> list[dict]:
    items = []
    for c_type in ("quiz", "flashcard", "summary", "exercise"):
        review_table, approved_table = _get_table_names(c_type)
        res = supabase.table(review_table).select("*").eq("content_source_id", source_id).eq("organization_id", organization_id).execute()
        for row in (res.data or []):
            items.append(_to_legacy_format(row, c_type, "pending_review"))

    for c_type in ("quiz", "flashcard", "summary", "exercise"):
        review_table, approved_table = _get_table_names(c_type)
        res = supabase.table(approved_table).select("*").eq("content_source_id", source_id).eq("organization_id", organization_id).execute()
        for row in (res.data or []):
            items.append(_to_legacy_format(row, c_type, "approved"))

    items.sort(key=lambda x: (x["content_type"], -x["version"]))
    return items


def get_review_queue(organization_id: str) -> list[dict]:
    items = []
    for c_type in ("quiz", "flashcard", "summary", "exercise"):
        review_table, _ = _get_table_names(c_type)
        res = supabase.table(review_table).select("*, content_sources(filename, source_type)").eq("organization_id", organization_id).execute()
        for row in (res.data or []):
            items.append(_to_legacy_format(row, c_type, "pending_review"))
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


def set_review_decision(content_id: str, organization_id: str, decision: str,
                        reviewer_id: str, notes: Optional[str] = None) -> dict:
    c_type, row_data = _find_review_item(content_id, organization_id)
    if not c_type or not row_data:
        return {}
        
    review_table, approved_table = _get_table_names(c_type)
    supabase.table(review_table).delete().eq("id", content_id).eq("organization_id", organization_id).execute()
    
    if decision == "approved":
        row_data["reviewed_by"] = reviewer_id
        row_data["reviewed_at"] = _now()
        supabase.table(approved_table).insert(row_data).execute()
        
    _log_audit(content_id, decision, reviewer_id, notes)
    return _to_legacy_format(row_data, c_type, decision)


def set_batch_review_decision(content_ids: list[str], organization_id: str, decision: str,
                              reviewer_id: str, notes: Optional[str] = None) -> int:
    count = 0
    for cid in content_ids:
        c_type, row_data = _find_review_item(cid, organization_id)
        if not c_type or not row_data:
            continue
            
        review_table, approved_table = _get_table_names(c_type)
        supabase.table(review_table).delete().eq("id", cid).eq("organization_id", organization_id).execute()
        
        if decision == "approved":
            row_data["reviewed_by"] = reviewer_id
            row_data["reviewed_at"] = _now()
            supabase.table(approved_table).insert(row_data).execute()
            
        _log_audit(cid, decision, reviewer_id, notes)
        count += 1
    return count


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
