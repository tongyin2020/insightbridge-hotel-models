import os, json, requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path("/Users/tongyin/Desktop/Hotel Model Rvisions/.env"))
key = os.getenv("MAKCORPS_API_KEY", "")

# Step 1: resolve the city name to a numeric document_id via /mapping
m = requests.get("https://api.makcorps.com/mapping",
                 params={"api_key": key, "name": "Macau"}, timeout=12)
print(f"/mapping: {m.status_code}")
if m.status_code != 200:
    print(m.text[:300]); raise SystemExit

results = m.json()
print(json.dumps(results, indent=2)[:600])

# pick the first GEO (city) result
geo = next((r for r in results if r.get("type") == "GEO"), results[0])
cityid = geo["document_id"]
print(f"\nresolved cityid = {cityid} ({geo.get('name')})")

# Step 2: city-wide price comparison using the numeric cityid
c = requests.get("https://api.makcorps.com/city",
                 params={"cityid": cityid, "api_key": key, "cur": "USD",
                         "rooms": 1, "adults": 2, "pagination": 0,
                         "checkin": "2026-05-31", "checkout": "2026-06-01"},
                 timeout=15)
print(f"\n/city: {c.status_code}")
print(c.text[:800] if c.status_code != 200 else json.dumps(c.json(), indent=2)[:800])
