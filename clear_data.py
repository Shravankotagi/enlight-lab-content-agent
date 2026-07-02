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

def main():
    tables = [
        "review_quizzes", "approved_quizzes",
        "review_flashcards", "approved_flashcards",
        "review_summaries", "approved_summaries",
        "review_exercises", "approved_exercises",
        "content_audit_log"
    ]

    for table in tables:
        print(f"Clearing table: {table}...")
        # Deletes all rows where id is not null (deletes all records)
        supabase.table(table).delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
        print(f"Cleared {table}.")

    print("All test data cleared successfully!")

if __name__ == "__main__":
    main()
