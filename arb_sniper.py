import requests
import time
from datetime import datetime, timezone, timedelta

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
API_KEYS = [
    '4ef677887a0eafc8e8d8439e56f38fbe',
    '41308dc8cb155421b36bf4e58a0fe50b',
    'dd7355f3917f77627fa683748e94db34',
    '5df6c4dcbe6da9ca2cdb1eeff8db6bdf',
    'c5c52b9e809325a878f4bd924039e127',
    '0c6838f456658cb5a1c7883bccdee8a2',
    '2c494c274c8fe8d3e81b780701f6f3b0'
]

NTFY_CHANNEL = 'nikunj_arb_alerts_2026' 
TOTAL_BANKROLL = 1500  

MIN_EV_THRESHOLD = 1.5  
MIN_ARB_THRESHOLD = 1.0

MY_BOOKIES = 'pinnacle,onexbet,marathonbet,dafabet,stake,betfair_ex_eu,betway'

TARGET_SPORTS = [
    'soccer_epl', 'soccer_uefa_champs_league', 
    'basketball_nba', 'icehockey_nhl', 
    'tennis_atp', 'tennis_wta'
]

current_key_index = 0
requests_remaining = "Unknown"
requests_used_total = "Unknown"
scan_starting_used = None

def get_active_api_key(): return API_KEYS[current_key_index]

def rotate_api_key():
    global current_key_index, scan_starting_used
    current_key_index += 1
    if current_key_index >= len(API_KEYS):
        print("❌ CRITICAL ERROR: All 7 API keys exhausted!")
        return False
    print(f"🔄 Quota reached! Switched to API Key #{current_key_index + 1}")
    scan_starting_used = None 
    return True

def send_phone_alert(message, percent, match_name, alert_type):
    try:
        emoji = "🚨" if alert_type == "ARB" else "📈"
        payload = {
            "topic": NTFY_CHANNEL, "message": message,
            "title": f"{emoji} {percent:.2f}% {alert_type} | {match_name}",
            "tags": ["gem", "moneybag"], "priority": 5 
        }
        requests.post("https://ntfy.sh/", json=payload)
    except: pass

def format_time_ist(iso_string):
    try:
        dt_utc = datetime.strptime(iso_string, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")
    except: return "Unknown Time"

def display_bookie(api_key):
    mapping = {'onexbet': '1xBet/Melbet', 'pinnacle': 'Pinnacle', 'marathonbet': 'Marathonbet', 'dafabet': 'Dafabet', 'stake': 'Stake.com', 'betfair_ex_eu': 'Betfair Exchange', 'betway': 'Betway'}
    return mapping.get(api_key, api_key.title())

def remove_vig(odds1, odds2):
    imp1, imp2 = 1/odds1, 1/odds2
    margin = imp1 + imp2
    return (1 / (imp1 / margin)), (1 / (imp2 / margin))

def calculate_kelly(soft_odds, true_odds, bankroll):
    b = soft_odds - 1.0
    p = 1.0 / true_odds
    q = 1.0 - p
    safe_kelly = ((b * p - q) / b) * 0.30 
    if safe_kelly <= 0: return 0
    if safe_kelly > 0.05: safe_kelly = 0.05
    return max(20, bankroll * safe_kelly)

def calculate_green_up(back_stake, back_odds, lay_odds):
    target_lay_stake = (back_stake * back_odds) / lay_odds
    guaranteed_profit = target_lay_stake - back_stake
    return target_lay_stake, guaranteed_profit

def extract_hybrid_data(bookmakers_list, target_bookies):
    ev_lines = {}; arb_lines = {}
    for bookie in bookmakers_list:
        b_name = bookie['key']
        if b_name not in target_bookies: continue
        for market in bookie.get('markets', []):
            if market['key'] in ['totals', 'spreads']:
                m_type = market['key'].upper()
                for outcome in market['outcomes']:
                    point = str(outcome.get('point', '0'))
                    name = outcome['name']
                    price = outcome['price']
                    if b_name == 'betfair_ex_eu': price = 1 + (price - 1) * 0.97
                    line_key = f"{m_type}_{point}"
                    
                    if line_key not in ev_lines: ev_lines[line_key] = {'pinnacle': {}, 'best_soft': {}}
                    if b_name == 'pinnacle': ev_lines[line_key]['pinnacle'][name] = price
                    elif b_name != 'pinnacle':
                        if name not in ev_lines[line_key]['best_soft'] or price > ev_lines[line_key]['best_soft'][name]['price']:
                            ev_lines[line_key]['best_soft'][name] = {'price': price, 'bookie': b_name}
                            
                    if line_key not in arb_lines: arb_lines[line_key] = {}
                    if name not in arb_lines[line_key] or price > arb_lines[line_key][name]['price']:
                        arb_lines[line_key][name] = {'price': price, 'bookie': b_name}
    return ev_lines, arb_lines

def evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport):
    found_evs, found_arbs = [], []
    for line_key, data in ev_lines.items():
        pinny, softs = data['pinnacle'], data['best_soft']
        if len(pinny) == 2:
            s1, s2 = list(pinny.keys())[0], list(pinny.keys())[1]
            t_odds1, t_odds2 = remove_vig(pinny[s1], pinny[s2])
            for side, true_odds in [(s1, t_odds1), (s2, t_odds2)]:
                if side in softs and softs[side]['price'] > true_odds:
                    ev_pct = ((softs[side]['price'] / true_odds) - 1) * 100
                    if ev_pct >= MIN_EV_THRESHOLD:
                        stake = calculate_kelly(softs[side]['price'], true_odds, TOTAL_BANKROLL)
                        t_lay, g_profit = calculate_green_up(stake, softs[side]['price'], true_odds)
                        found_evs.append({
                            'pct': ev_pct, 'match': match_name, 'time': match_time, 'sport': sport,
                            'line': line_key, 'selection': side, 'odds': softs[side]['price'], 
                            'true': true_odds, 'bookie': softs[side]['bookie'],
                            'stake': stake, 'target_lay': t_lay, 'green_profit': g_profit
                        })

    for line_key, outcomes in arb_lines.items():
        if len(outcomes) == 2:
            k1, k2 = list(outcomes.keys())[0], list(outcomes.keys())[1]
            margin = (1 / outcomes[k1]['price']) + (1 / outcomes[k2]['price'])
            if margin < 1.0:
                arb_pct = (1 - margin) * 100
                if arb_pct >= MIN_ARB_THRESHOLD:
                    found_arbs.append({
                        'pct': arb_pct, 'match': match_name, 'time': match_time, 'sport': sport, 'line': line_key,
                        's1': k1, 's1_data': outcomes[k1], 's2': k2, 's2_data': outcomes[k2],
                        'stk1': (TOTAL_BANKROLL / margin) / outcomes[k1]['price'],
                        'stk2': (TOTAL_BANKROLL / margin) / outcomes[k2]['price'],
                        'profit': (TOTAL_BANKROLL / margin) - TOTAL_BANKROLL
                    })
    return found_evs, found_arbs

def fetch_odds_with_retry(url, params):
    global requests_remaining, requests_used_total, scan_starting_used
    while True:
        params['apiKey'] = get_active_api_key()
        res = requests.get(url, params=params)
        
        if 'x-requests-remaining' in res.headers: requests_remaining = res.headers['x-requests-remaining']
        if 'x-requests-used' in res.headers:
            requests_used_total = res.headers['x-requests-used']
            if scan_starting_used is None: scan_starting_used = int(requests_used_total) - 2 

        if res.status_code == 401:
            if rotate_api_key(): continue 
            else: return None 
        elif res.status_code == 429:
            if 'quota' in res.json().get('message', '').lower():
                if rotate_api_key(): continue
                else: return None
            else: time.sleep(2); continue
        elif res.status_code == 200: return res.json()
        else: return None 

def generate_web_dashboard(evs, arbs, current_time):
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Arb Sniper Live</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }}
            h1 {{ color: #58a6ff; text-align: center; font-size: 24px; }}
            h2 {{ color: #ffffff; border-bottom: 1px solid #30363d; padding-bottom: 5px; margin-top: 30px; }}
            .time {{ text-align: center; color: #8b949e; font-size: 14px; margin-bottom: 30px; }}
            .card {{ background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
            .card.ev {{ border-left: 5px solid #238636; }}
            .card.arb {{ border-left: 5px solid #da3633; }}
            .edge {{ font-size: 18px; font-weight: bold; margin-bottom: 10px; }}
            .ev-edge {{ color: #3fb950; }}
            .arb-edge {{ color: #ff7b72; }}
            .match {{ font-size: 16px; font-weight: 600; color: #ffffff; margin-bottom: 5px; }}
            .details {{ font-size: 14px; color: #8b949e; line-height: 1.5; }}
            .highlight {{ color: #ffffff; font-weight: bold; }}
            .telemetry {{ text-align: center; margin-top: 40px; font-size: 12px; color: #484f58; }}
        </style>
    </head>
    <body>
        <h1>📡 Arb Sniper Master Dashboard</h1>
        <div class="time">Last Scan: {current_time} (IST)</div>
    """

    if not evs and not arbs:
        html += '<div class="card" style="text-align: center;">✅ The global sports market is perfectly balanced right now. No massive edges found.</div>'

    if evs:
        html += f"<h2>💎 Expected Value ({len(evs)})</h2>"
        for ev in evs:
            html += f"""
            <div class="card ev">
                <div class="edge ev-edge">📈 {ev['pct']:.2f}% EV Edge</div>
                <div class="match">{ev['match']}</div>
                <div class="details">
                    👉 Bet: <span class="highlight">{ev['selection'].upper()} {ev['line'].split('_')[1]} @ {ev['odds']:.2f}</span> on {display_bookie(ev['bookie'])}<br>
                    🧠 True Odds: {ev['true']:.2f} | 💰 Safe Stake: <span class="highlight">₹{ev['stake']:.0f}</span><br>
                    📅 {ev['time']}
                </div>
            </div>
            """

    if arbs:
        html += f"<h2>🏆 Arbitrage ({len(arbs)})</h2>"
        for arb in arbs:
            html += f"""
            <div class="card arb">
                <div class="edge arb-edge">🚨 {arb['pct']:.2f}% ARB Edge</div>
                <div class="match">{arb['match']}</div>
                <div class="details">
                    🔵 Bet <span class="highlight">₹{arb['stk1']:.0f}</span> on {arb['s1'].upper()} @ {arb['s1_data']['price']:.2f} [{display_bookie(arb['s1_data']['bookie'])}]<br>
                    🔴 Bet <span class="highlight">₹{arb['stk2']:.0f}</span> on {arb['s2'].upper()} @ {arb['s2_data']['price']:.2f} [{display_bookie(arb['s2_data']['bookie'])}]<br>
                    ✨ Net Profit: <span class="highlight">₹{arb['profit']:.0f}</span><br>
                    📅 {arb['time']}
                </div>
            </div>
            """

    credits_burned = int(requests_used_total) - scan_starting_used if scan_starting_used is not None and str(requests_used_total).isdigit() else "Unknown"
    html += f"""
        <div class="telemetry">
            Key #{current_key_index + 1} | Monthly Quota Remaining: {requests_remaining}/500 | Scan Cost: ~{credits_burned} credits
        </div>
    </body>
    </html>
    """
    
    with open("index.html", "w") as f:
        f.write(html)
    print("🌐 Web Dashboard successfully updated (index.html)")

def run_hybrid_scanner():
    global scan_starting_used
    my_bookies_list = MY_BOOKIES.split(',')
    scan_starting_used = None 
    
    current_time_str = datetime.now().strftime('%H:%M:%S')
    print(f"\n📡 [{current_time_str}] ALL-SPORTS Sweep (EV + ARB) active...")
    all_evs, all_arbs = [], []

    for sport in TARGET_SPORTS:
        url = f'https://api.the-odds-api.com/v4/sports/{sport}/odds'
        params = {'regions': 'eu', 'bookmakers': MY_BOOKIES, 'markets': 'totals,spreads', 'oddsFormat': 'decimal'}
        events = fetch_odds_with_retry(url, params)
        if not events: continue 
            
        for event in events:
            match_name = f"{event['home_team']} vs {event['away_team']}"
            match_time = format_time_ist(event['commence_time'])
            ev_lines, arb_lines = extract_hybrid_data(event.get('bookmakers', []), my_bookies_list)
            new_evs, new_arbs = evaluate_markets(ev_lines, arb_lines, match_name, match_time, sport)
            all_evs.extend(new_evs)
            all_arbs.extend(new_arbs)
            
        time.sleep(1.5)
            
    # Display in console and push to phone
    if all_arbs:
        all_arbs.sort(key=lambda x: x['pct'], reverse=True)
        for arb in all_arbs:
            msg = f"🏆 {arb['sport'].replace('_', ' ').title()}\n📅 {arb['time']}\n📈 {arb['line']}\n\n🔵 ₹{arb['stk1']:.0f} on {arb['s1'].upper()} @ {arb['s1_data']['price']:.2f} [{display_bookie(arb['s1_data']['bookie'])}]\n🔴 ₹{arb['stk2']:.0f} on {arb['s2'].upper()} @ {arb['s2_data']['price']:.2f} [{display_bookie(arb['s2_data']['bookie'])}]\n\n✨ Profit: ₹{arb['profit']:.0f}"
            send_phone_alert(msg, arb['pct'], arb['match'], "ARB")

    if all_evs:
        all_evs.sort(key=lambda x: x['pct'], reverse=True)
        for ev in all_evs:
            msg = f"🏆 {ev['sport'].replace('_', ' ').title()}\n📅 {ev['time']}\n\n💰 BET EXACTLY: ₹{ev['stake']:.0f}\n👉 {ev['selection'].upper()} {ev['line'].split('_')[1]} @ {ev['odds']:.2f} on {display_bookie(ev['bookie'])}\n\n🧠 True Odds: {ev['true']:.2f}"
            send_phone_alert(msg, ev['pct'], ev['match'], "EV")

    # Generate the HTML Dashboard
    generate_web_dashboard(all_evs, all_arbs, current_time_str)

    # Telemetry
    print("\n" + "=" * 65)
    print("📊 API USAGE REPORT")
    print("=" * 65)
    print(f"🔑 Active Key Index: #{current_key_index + 1}")
    print(f"📉 Remaining Monthly Credits: {requests_remaining} / 500")
    print("=" * 65)

if __name__ == "__main__":
    print("🚀 GitHub Actions Master Cloud Engine Started...")
    run_hybrid_scanner()
    print("🏁 Scan complete.")
