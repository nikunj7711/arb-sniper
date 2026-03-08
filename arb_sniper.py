import os, json, requests, time, threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==========================================
#  CONFIGURATION (ULTRA-LOW COST MODE)
# ==========================================
_raw_keys = os.getenv('ODDS_API_KEYS', '')
API_KEYS = [k.strip() for k in _raw_keys.split(',') if k.strip()]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026'
TOTAL_BANKROLL = 1500
MIN_EV_THRESHOLD = 1.5
MIN_ARB_THRESHOLD = 1.0

# Using only the sharpest and softest books for maximum variance
MY_BOOKIES = 'pinnacle,onexbet,bet365,unibet,betway,stake,marathonbet'

# 🎯 THE SNIPER LIST: 6 Credits per scan. 
TARGET_SPORTS = [
    'soccer_epl', 
    'soccer_spain_la_liga', 
    'soccer_uefa_champs_league',
    'basketball_nba', 
    'icehockey_nhl', 
    'tennis_atp',
    'cricket_t20_world_cup' # <-- Delete this line tomorrow after the Final!
]

BOOK_CAPS = {
    'betway': 300, 'stake': 500, 'onexbet': 400, 'marathonbet': 400,  
    'pinnacle': 1000, 'bet365': 400, 'unibet': 350
}

# [Leave all your existing State Managers, Math Engine, and Website Generator exactly as they are below this line]

# ==========================================
#  CLOUD FETCHING (COST SAVING)
# ==========================================
def fetch_odds_with_retry(url, params):
    while True:
        key, idx = get_active_api_key()
        if not key: return None
        params['apiKey'] = key
        try:
            res = requests.get(url, params=params, timeout=15)
        except: return None
            
        rem = res.headers.get('x-requests-remaining')
        if rem: update_key_telemetry(idx, rem)

        if res.status_code in [401, 429]:
            if rotate_api_key(idx): continue
            else: return None
        elif res.status_code == 200:
            return res.json()
        return None

def fetch_all_sports():
    results = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        # 🛡️ SAVINGS: Requesting ONLY 'h2h' to prevent API from double-charging credits
        futures = {executor.submit(
            fetch_odds_with_retry, 
            f'https://api.the-odds-api.com/v4/sports/{sp}/odds', 
            {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'h2h'}
        ): sp for sp in TARGET_SPORTS}
        
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results
