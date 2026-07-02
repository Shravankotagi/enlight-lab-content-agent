import requests

URL = "http://localhost:8001"
HEADERS = {"X-API-Key": "8b4c91cf577747ad7b5da28e8ad08a9c009bb21bbe99c61d44671028a330087f"}
ORG_ID = "00000000-0000-0000-0000-000000000001"

def test_queue():
    print("Testing GET /content/review-queue...")
    r = requests.get(f"{URL}/content/review-queue?organization_id={ORG_ID}", headers=HEADERS)
    print("Status:", r.status_code)
    if r.status_code == 200:
        data = r.json()
        print(f"Returned {len(data)} review items.")
        if data:
            print("First item sample:", data[0]["content_type"], "ID:", data[0]["id"])
            return data[0]
    else:
        print("Error:", r.text)
    return None

def test_source_content(source_id):
    print(f"Testing GET /content/{source_id}...")
    r = requests.get(f"{URL}/content/{source_id}?organization_id={ORG_ID}", headers=HEADERS)
    print("Status:", r.status_code)
    if r.status_code == 200:
        data = r.json()
        print(f"Source: {data.get('source', {}).get('filename')}")
        print(f"Returned {len(data.get('content', []))} items.")
    else:
        print("Error:", r.text)

if __name__ == "__main__":
    first_item = test_queue()
    if first_item:
        test_source_content(first_item["source_id"])
