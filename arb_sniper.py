"""
ScraperAPI diagnostic for BC.Game sptpub.com endpoint.
Run locally — paste your ScraperAPI key below.
Tests all 4 ScraperAPI call modes to find which one works.
"""
import requests, json, urllib.parse

# ── PASTE YOUR KEY HERE ────────────────────────────────────────────────────────
SCRAPERAPI_KEY = "2e8c924e5f7b71d9f022c75d5a7ab033"
TARGET_URL     = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
INSOMNIA_UA    = "insomnia/12.4.0"

def check(label, url, **kwargs):
    try:
        r = requests.get(url, timeout=30, **kwargs)
        body = r.text[:300].replace('\n', ' ')
        ok = r.status_code == 200 and '{' in r.text
        print(f"  {'✅' if ok else '❌'}  {label}")
        print(f"      HTTP {r.status_code}  size={len(r.content)}B")
        print(f"      preview: {body}")
        return ok, r
    except Exception as e:
        print(f"  ❌  {label}")
        print(f"      ERROR: {e}")
        return False, None

enc = urllib.parse.quote(TARGET_URL, safe='')

print("=" * 65)
print("TEST 1 — Direct request (insomnia UA)")
print("=" * 65)
check("Direct insomnia UA",
      TARGET_URL,
      headers={"User-Agent": INSOMNIA_UA, "Accept": "application/json"})

print()
print("=" * 65)
print("TEST 2 — ScraperAPI API mode (no keep_headers)")
print("=" * 65)
check("ScraperAPI basic",
      f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={enc}&render=false")

print()
print("=" * 65)
print("TEST 3 — ScraperAPI API mode + keep_headers=true")
print("=" * 65)
check("ScraperAPI keep_headers",
      f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={enc}&keep_headers=true&render=false",
      headers={"User-Agent": INSOMNIA_UA, "Accept": "application/json"})

print()
print("=" * 65)
print("TEST 4 — ScraperAPI proxy mode (8001)")
print("=" * 65)
proxy = f"http://scraperapi:{SCRAPERAPI_KEY}@proxy-server.scraperapi.com:8001"
check("ScraperAPI proxy 8001",
      TARGET_URL,
      proxies={"http": proxy, "https": proxy},
      headers={"User-Agent": INSOMNIA_UA, "Accept": "application/json"},
      verify=False)

print()
print("=" * 65)
print("TEST 5 — ScraperAPI proxy mode (8080)")
print("=" * 65)
proxy80 = f"http://scraperapi:{SCRAPERAPI_KEY}@proxy-server.scraperapi.com:8080"
check("ScraperAPI proxy 8080",
      TARGET_URL,
      proxies={"http": proxy80, "https": proxy80},
      headers={"User-Agent": INSOMNIA_UA, "Accept": "application/json"},
      verify=False)

print()
print("=" * 65)
print("TEST 6 — ScraperAPI account check (quota remaining)")
print("=" * 65)
try:
    r = requests.get(f"https://api.scraperapi.com/account?api_key={SCRAPERAPI_KEY}", timeout=10)
    print(f"  Account: {r.text[:200]}")
except Exception as e:
    print(f"  ERROR: {e}")
