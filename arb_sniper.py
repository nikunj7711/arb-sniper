import requests
from datetime import datetime, timezone

def test_bc_pipeline():
    headers = {
        'accept': '*/*',
        'origin': 'https://bc.game',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    print("🥷 Pinging BC.Game Servers...")
    try:
        # Step 1
        map_url = 'https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0'
        map_res = requests.get(map_url, headers=headers, timeout=10)
        version = map_res.json()['top_events_versions'][0]
        print(f"✅ Map found: {version}")
        
        # Step 2
        data_url = f"https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/{version}"
        events = requests.get(data_url, headers=headers, timeout=10).json().get('events', {})
        
        print(f"✅ Downloaded {len(events)} total matches.")
        
        # Print the first 10 matches so we can see the exact team names
        print("\n🔍 FIRST 10 MATCHES FOUND:")
        count = 0
        for match in events.values():
            desc = match.get('desc', {})
            comps = desc.get('competitors', [])
            if len(comps) >= 2:
                print(f" - {comps[0].get('name')} vs {comps[1].get('name')}")
                count += 1
            if count >= 10: break
                
    except Exception as e:
        print(f"❌ ERROR: {e}")

if __name__ == "__main__":
    test_bc_pipeline()
