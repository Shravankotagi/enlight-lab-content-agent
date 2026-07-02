import db
import json

items = db.supabase.table('generated_content').select('*').eq('status', 'pending_review').execute().data
for item in items:
    if item['content_type'] in ['exercise', 'summary']:
        print(f"ID: {item['id']}, Type: {item['content_type']}, Format: {item['format']}, Keys: {list(item['payload'].keys())}")
        print("Payload:", json.dumps(item['payload'], indent=2))
        print("="*60)
