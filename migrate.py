import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERROR] Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def infer_content_type(row):
    c_type = row.get("content_type")
    if c_type:
        return c_type
    
    payload = row.get("payload") or {}
    if "question" in payload:
        return "quiz"
    elif "front" in payload or "back" in payload:
        return "flashcard"
    elif "summary" in payload:
        return "summary"
    elif "task" in payload or "case_study" in payload or "scenario" in payload:
        return "exercise"
    
    return "unknown"


def main():
    print("Fetching legacy generated_content rows...")
    res = supabase.table("generated_content").select("*").execute()
    rows = res.data or []
    print(f"Loaded {len(rows)} legacy items.")

    stats = {
        "review_quizzes": 0, "approved_quizzes": 0,
        "review_flashcards": 0, "approved_flashcards": 0,
        "review_summaries": 0, "approved_summaries": 0,
        "review_exercises": 0, "approved_exercises": 0,
        "skipped_rejected": 0, "unknown_type": 0
    }

    for row in rows:
        status = row.get("status", "draft")
        if status == "rejected":
            stats["skipped_rejected"] += 1
            continue

        c_type = infer_content_type(row)
        if c_type == "unknown":
            print(f"[WARN] Could not infer type for item ID: {row['id']}")
            stats["unknown_type"] += 1
            continue

        prefix = "approved_" if status == "approved" else "review_"
        
        # Pluralize correctly
        if c_type == "quiz":
            plural = "quizzes"
        elif c_type == "exercise":
            plural = "exercises"
        elif c_type == "summary":
            plural = "summaries"
        elif c_type == "flashcard":
            plural = "flashcards"
        else:
            print(f"[WARN] Unknown type: {c_type}")
            stats["unknown_type"] += 1
            continue

        table = f"{prefix}{plural}"
        
        payload = row.get("payload") or {}

        # Prepare base fields
        data = {
            "id": row["id"],
            "content_source_id": row["source_id"],
            "organization_id": row["organization_id"],
            "course_id": row.get("course_id"),
            "bloom_level": row.get("bloom_level"),
            "quality_score": row.get("quality_score"),
            "version": row.get("version", 1),
            "created_at": row.get("created_at")
        }

        if status == "approved":
            data["reviewed_by"] = row.get("reviewed_by")
            data["reviewed_at"] = row.get("reviewed_at")

        # Type-specific mapping
        if c_type == "quiz":
            data["question"] = payload.get("question") or "Quiz Question"
            # Format/answer_type check
            fmt = row.get("format") or ("mcq" if "options" in payload else "open_ended")
            data["answer_type"] = fmt
            data["options"] = payload.get("options")
            data["correct_answer"] = payload.get("correct_answer") or payload.get("answer")

        elif c_type == "flashcard":
            data["front"] = payload.get("front") or payload.get("term") or payload.get("concept") or payload.get("question") or payload.get("title") or payload.get("prompt") or "Flashcard Term"
            data["back"] = payload.get("back") or payload.get("definition") or payload.get("explanation") or payload.get("answer") or payload.get("details") or payload.get("text") or "Flashcard Definition"

        elif c_type == "summary":
            data["summary"] = payload.get("summary") or payload.get("overview") or payload.get("text") or payload.get("description") or payload.get("details") or "Summary Overview"
            data["key_takeaways"] = payload.get("key_takeaways") or payload.get("key_points")

        elif c_type == "exercise":
            data["title"] = payload.get("title") or "Practice Exercise"
            data["task"] = payload.get("prompt") or payload.get("task") or payload.get("question") or payload.get("scenario") or "Exercise Prompt"
            
            fmt = row.get("format")
            if fmt == "case_study" or "scenario" in payload:
                data["case_study"] = {"scenario": payload.get("scenario")}
            else:
                data["case_study"] = None

        print(f"Migrating {row['id']} ({c_type}, status={status}) -> {table}")
        supabase.table(table).insert(data).execute()
        stats[table] += 1

    print("\nMigration Stats:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print("\nMigration completed successfully!")


if __name__ == "__main__":
    main()
