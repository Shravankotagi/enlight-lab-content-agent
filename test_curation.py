from dotenv import load_dotenv
load_dotenv()

import asyncio
import ai_client

async def test():
    print("Testing curation...")
    learning_objectives = ["Understand binary search trees", "Analyze runtime complexity"]
    key_concepts = ["B-Trees", "Big O notation", "Tree traversal"]
    
    # This should fall back to Gemini model suggestions if the search grounding tool quota is exhausted
    refs = await ai_client.curate_external_content(learning_objectives, key_concepts)
    print("Curated References:")
    for r in refs:
        print(f" - Title: {r.get('title')}")
        print(f"   URL: {r.get('url')}")
        print(f"   Description: {r.get('description')}")
        print(f"   Relevance: {r.get('relevance_score')}")

if __name__ == "__main__":
    asyncio.run(test())
