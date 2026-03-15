#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ARB SNIPER v3.0 — QUANT ARBITRAGE ENGINE                 ║
║         Global Sports Arbitrage + EV Scanner | Dashboard Generator          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os, json, math, time, hashlib, requests, logging, itertools
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ArbSniper")

# ─── Constants ─────────────────────────────────────────────────────────────────
ODDS_API_KEY    = "f633acbe8cbbafe5f9890a0decb4fc2c"
ODDS_BASE       = "https://api.the-odds-api.com/v4"
BCGAME_URL      = "https://api-k-c7818b61-623.sptpub.com/api/v4/prematch/brand/2103509236163162112/en/0"
NTFY_URL        = "https://ntfy.sh/nikunj_arb_alerts_2026"
STATE_FILE      = "api_state.json"
OUTPUT_HTML     = "index.html"
DASHBOARD_PASS  = os.environ.get("DASHBOARD_PASS", "arb2026")
KELLY_FRACTION  = 0.30
MIN_ARB_PROFIT  = 0.001   # 0.1%
MIN_EV_EDGE     = 0.005   # 0.5%
BANK_SIZE       = 10000   # Default bank in ₹ for Kelly calc

ALLOWED_BOOKS = {
    "pinnacle", "onexbet", "bet365", "unibet", "betway", "stake",
    "marathonbet", "parimatch", "betfair", "dafabet", "bovada",
    "draftkings", "fanduel", "betmgm"
}

SPORTS_LIST = [
    "soccer_epl","soccer_spain_la_liga","soccer_germany_bundesliga","soccer_italy_serie_a",
    "soccer_france_ligue_one","soccer_uefa_champs_league","soccer_uefa_europa_league",
    "soccer_brazil_campeonato","soccer_argentina_primera_division","soccer_turkey_super_league",
    "basketball_nba","basketball_euroleague","icehockey_nhl","tennis_atp_french_open",
    "tennis_wta_french_open","mma_mixed_martial_arts","americanfootball_nfl",
    "cricket_ipl","cricket_international_championship","rugby_union_world_cup",
    "boxing_boxing","baseball_mlb","aussierules_afl","golf_masters_tournament_winner"
]

MARKETS = ["h2h", "totals", "spreads"]
REGIONS  = "eu,uk,us,au"

# ─── State Management ──────────────────────────────────────────────────────────
def load_state():
    defaults = {"remaining_requests": 500, "used_today": 0, "last_reset": str(datetime.utcnow().date())}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                defaults.update(saved)   # saved values win, but defaults fill any missing keys
        except Exception:
            pass
    return defaults

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─── The Odds API ──────────────────────────────────────────────────────────────
def fetch_sport_odds(sport: str, market: str, state: dict) -> list:
    if state.get("remaining_requests", 0) < 5:
        log.warning(f"API quota low ({state['remaining_requests']} left). Skipping {sport}/{market}.")
        return []
    url = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": market,
        "oddsFormat": "decimal",
        "dateFormat": "iso"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        # Update state from response headers
        if "X-Requests-Remaining" in r.headers:
            state["remaining_requests"] = int(r.headers["X-Requests-Remaining"])
        if "X-Requests-Used" in r.headers:
            state["used_today"] = int(r.headers["X-Requests-Used"])
        if r.status_code == 429:
            log.error("Rate limit hit!")
            return []
        if r.status_code != 200:
            log.warning(f"API error {r.status_code} for {sport}/{market}")
            return []
        data = r.json()
        # Filter bookmakers locally
        filtered = []
        for event in data:
            bms = [b for b in event.get("bookmakers", []) if b["key"] in ALLOWED_BOOKS]
            if bms:
                event["bookmakers"] = bms
                filtered.append(event)
        log.info(f"  [{sport}/{market}] {len(filtered)} events | Remaining API: {state['remaining_requests']}")
        return filtered
    except Exception as e:
        log.error(f"Fetch error {sport}/{market}: {e}")
        return []

def fetch_all_odds(state: dict) -> list:
    all_events = []
    tasks = [(s, m) for s in SPORTS_LIST for m in MARKETS]
    log.info(f"Fetching {len(tasks)} sport/market combos concurrently...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_sport_odds, s, m, state): (s, m) for s, m in tasks}
        for fut in as_completed(futures):
            result = fut.result()
            all_events.extend(result)
    return all_events

# ─── BC.Game Scraper ───────────────────────────────────────────────────────────
def fetch_bcgame_events() -> list:
    try:
        r = requests.get(BCGAME_URL, timeout=20)
        if r.status_code != 200:
            log.warning(f"BC.Game API returned {r.status_code}")
            return []
        raw = r.json()
        # Navigate the response structure
        events_raw = []
        if isinstance(raw, dict):
            for key in ["data", "events", "list", "items"]:
                if key in raw:
                    events_raw = raw[key]
                    break
        elif isinstance(raw, list):
            events_raw = raw

        converted = []
        for ev in events_raw[:200]:  # Cap at 200 to avoid overload
            try:
                home = ev.get("homeTeam", ev.get("home", ev.get("team1", "")))
                away = ev.get("awayTeam", ev.get("away", ev.get("team2", "")))
                sport = ev.get("sportName", ev.get("sport", "Unknown"))
                commence = ev.get("startTime", ev.get("startAt", ev.get("time", "")))
                markets = ev.get("markets", ev.get("odds", []))

                outcomes = []
                if isinstance(markets, list):
                    for mkt in markets:
                        if isinstance(mkt, dict):
                            for o in mkt.get("outcomes", mkt.get("selections", [])):
                                outcomes.append({
                                    "name": o.get("name", o.get("label", "")),
                                    "price": float(o.get("price", o.get("odds", 2.0)))
                                })

                if home and away and outcomes:
                    converted.append({
                        "id": f"bcgame_{hash(home+away)}",
                        "sport_title": sport,
                        "home_team": home,
                        "away_team": away,
                        "commence_time": str(commence),
                        "bookmakers": [{
                            "key": "bcgame",
                            "title": "BC.Game",
                            "last_update": str(datetime.utcnow().isoformat()),
                            "markets": [{"key": "h2h", "outcomes": outcomes}]
                        }]
                    })
            except Exception:
                continue
        log.info(f"BC.Game: {len(converted)} events parsed.")
        return converted
    except Exception as e:
        log.error(f"BC.Game fetch failed: {e}")
        return []

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def merge_bcgame(odds_events: list, bc_events: list) -> list:
    """Merge BC.Game events into main events list by fuzzy team name matching."""
    merged_count = 0
    for bc_ev in bc_events:
        bc_home = bc_ev["home_team"]
        bc_away = bc_ev["away_team"]
        best_match = None
        best_score = 0.0
        for ev in odds_events:
            ev_home = ev.get("home_team", "")
            ev_away = ev.get("away_team", "")
            score = (similarity(bc_home, ev_home) + similarity(bc_away, ev_away)) / 2
            if score > best_score:
                best_score = score
                best_match = ev
        if best_score > 0.72 and best_match:
            best_match["bookmakers"].extend(bc_ev["bookmakers"])
            merged_count += 1
        else:
            odds_events.append(bc_ev)
    log.info(f"BC.Game merge: {merged_count} events merged, rest appended standalone.")
    return odds_events

# ─── Quant Math Engine ─────────────────────────────────────────────────────────
def remove_vig_pinnacle(pinnacle_outcomes: list) -> dict:
    """Remove vig from Pinnacle odds using multiplicative method. Returns {name: true_prob}."""
    raw_probs = {o["name"]: 1.0 / o["price"] for o in pinnacle_outcomes}
    total_prob = sum(raw_probs.values())
    if total_prob <= 0:
        return {}
    true_probs = {name: p / total_prob for name, p in raw_probs.items()}
    return true_probs

def true_odds_from_prob(prob: float) -> float:
    if prob <= 0 or prob >= 1:
        return 0.0
    return 1.0 / prob

def kelly_stake(edge: float, odds: float, bank: float, fraction: float = KELLY_FRACTION) -> float:
    """Kelly Criterion: f = (bp - q) / b where b = odds-1, p = win prob, q = 1-p"""
    b = odds - 1.0
    if b <= 0:
        return 0.0
    p = 1.0 / (odds / (1.0 + edge))  # Implied true prob from edge-adjusted odds
    q = 1.0 - p
    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0
    return round(fraction * kelly_full * bank, 2)

def round_to_nearest(amount: float, nearest: float = 10.0) -> float:
    return round(round(amount / nearest) * nearest, 2)

def calculate_arb_stakes(odds_list: list, total_stake: float = 1000.0) -> list:
    """Calculate individual stakes for an arbitrage bet."""
    total_implied = sum(1.0 / o for o in odds_list)
    if total_implied >= 1.0:
        return [0.0] * len(odds_list)
    stakes = [(1.0 / o) / total_implied * total_stake for o in odds_list]
    return stakes

def scan_arbitrage(events: list) -> list:
    arbs = []
    for ev in events:
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        sport = ev.get("sport_title", "Unknown")
        commence = ev.get("commence_time", "")

        for market_key in MARKETS:
            # Collect best odds per outcome across all bookmakers
            best_odds: dict = {}  # outcome_name -> (price, bookmaker)
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt["key"] != market_key:
                        continue
                    for outcome in mkt.get("outcomes", []):
                        name = outcome.get("name", "")
                        # Normalize spread outcomes by abs(point)
                        point = outcome.get("point", None)
                        if point is not None:
                            name = f"{name}_{abs(float(point))}"
                        price = float(outcome.get("price", 0))
                        if price <= 1.0:
                            continue
                        if name not in best_odds or price > best_odds[name][0]:
                            best_odds[name] = (price, bm["title"], bm["key"])

            if len(best_odds) < 2:
                continue

            # For h2h: check 2-way and 3-way
            outcomes_list = list(best_odds.items())

            # Check all 2-way combos
            for combo in itertools.combinations(outcomes_list, 2):
                names   = [c[0] for c in combo]
                prices  = [c[1][0] for c in combo]
                books   = [c[1][1] for c in combo]
                bk_keys = [c[1][2] for c in combo]
                total_impl = sum(1.0/p for p in prices)
                if total_impl < 1.0:
                    profit_pct = (1.0 / total_impl - 1.0)
                    if profit_pct >= MIN_ARB_PROFIT:
                        stakes = calculate_arb_stakes(prices, 1000)
                        arbs.append({
                            "type": "Arbitrage",
                            "market": market_key.upper(),
                            "sport": sport,
                            "match": f"{home} vs {away}",
                            "commence": commence,
                            "outcomes": [
                                {"name": n, "odds": p, "book": b, "book_key": bk,
                                 "stake": round(s, 2), "stake_rounded": round_to_nearest(s)}
                                for n, p, b, bk, s in zip(names, prices, books, bk_keys, stakes)
                            ],
                            "total_implied": round(total_impl, 4),
                            "profit_pct": round(profit_pct * 100, 3),
                            "profit_amt": round((profit_pct) * 1000, 2),
                            "ways": 2
                        })

            # Check 3-way
            if len(outcomes_list) >= 3:
                for combo in itertools.combinations(outcomes_list, 3):
                    names   = [c[0] for c in combo]
                    prices  = [c[1][0] for c in combo]
                    books   = [c[1][1] for c in combo]
                    bk_keys = [c[1][2] for c in combo]
                    total_impl = sum(1.0/p for p in prices)
                    if total_impl < 1.0:
                        profit_pct = (1.0 / total_impl - 1.0)
                        if profit_pct >= MIN_ARB_PROFIT:
                            stakes = calculate_arb_stakes(prices, 1000)
                            arbs.append({
                                "type": "Arbitrage",
                                "market": market_key.upper(),
                                "sport": sport,
                                "match": f"{home} vs {away}",
                                "commence": commence,
                                "outcomes": [
                                    {"name": n, "odds": p, "book": b, "book_key": bk,
                                     "stake": round(s, 2), "stake_rounded": round_to_nearest(s)}
                                    for n, p, b, bk, s in zip(names, prices, books, bk_keys, stakes)
                                ],
                                "total_implied": round(total_impl, 4),
                                "profit_pct": round(profit_pct * 100, 3),
                                "profit_amt": round((profit_pct) * 1000, 2),
                                "ways": 3
                            })

    arbs.sort(key=lambda x: x["profit_pct"], reverse=True)
    log.info(f"Arbitrage scan: {len(arbs)} opportunities found.")
    return arbs

def scan_ev_bets(events: list) -> list:
    ev_bets = []
    for ev in events:
        home  = ev.get("home_team", "")
        away  = ev.get("away_team", "")
        sport = ev.get("sport_title", "Unknown")
        commence = ev.get("commence_time", "")

        for market_key in MARKETS:
            # Find Pinnacle lines first
            pinnacle_outcomes = None
            for bm in ev.get("bookmakers", []):
                if bm["key"] == "pinnacle":
                    for mkt in bm.get("markets", []):
                        if mkt["key"] == market_key:
                            pinnacle_outcomes = mkt.get("outcomes", [])
                            break
                if pinnacle_outcomes:
                    break

            if not pinnacle_outcomes or len(pinnacle_outcomes) < 2:
                continue

            true_probs = remove_vig_pinnacle(pinnacle_outcomes)
            if not true_probs:
                continue

            # Compare every soft bookie outcome vs true odds
            for bm in ev.get("bookmakers", []):
                if bm["key"] == "pinnacle":
                    continue
                for mkt in bm.get("markets", []):
                    if mkt["key"] != market_key:
                        continue
                    for outcome in mkt.get("outcomes", []):
                        name  = outcome.get("name", "")
                        price = float(outcome.get("price", 0))
                        if price <= 1.0 or name not in true_probs:
                            continue
                        true_prob     = true_probs[name]
                        true_odd      = true_odds_from_prob(true_prob)
                        edge          = (price - true_odd) / true_odd
                        if edge >= MIN_EV_EDGE:
                            ev_pct  = edge * 100
                            k_stake = kelly_stake(edge, price, BANK_SIZE)
                            ev_bets.append({
                                "type": "Value Bet",
                                "market": market_key.upper(),
                                "sport": sport,
                                "match": f"{home} vs {away}",
                                "commence": commence,
                                "outcome": name,
                                "book": bm["title"],
                                "book_key": bm["key"],
                                "offered_odds": round(price, 3),
                                "true_odds": round(true_odd, 3),
                                "true_prob_pct": round(true_prob * 100, 2),
                                "edge_pct": round(ev_pct, 3),
                                "kelly_stake": k_stake,
                                "kelly_stake_rounded": round_to_nearest(k_stake)
                            })

    ev_bets.sort(key=lambda x: x["edge_pct"], reverse=True)
    log.info(f"EV scan: {len(ev_bets)} value bets found.")
    return ev_bets

# ─── Push Notification ─────────────────────────────────────────────────────────
def send_push(arbs: list, evs: list):
    if not arbs and not evs:
        log.info("No opportunities found. Skipping push notification.")
        return
    top = arbs[0] if arbs else None
    msg = ""
    if top:
        msg = (f"TOP ARB: {top['match']} | {top['profit_pct']}% profit | "
               f"{top['market']} | {top['ways']}-way | "
               f"EV bets found: {len(evs)}")
    elif evs:
        top_ev = evs[0]
        msg = f"TOP EV: {top_ev['match']} | {top_ev['edge_pct']}% edge | {top_ev['book']} | Total EVs: {len(evs)}"
    try:
        headers = {
            "Title": "Arb Sniper Alert",
            "Priority": "high",
            "Tags": "zap,moneybag",
            "Content-Type": "text/plain; charset=utf-8"
        }
        r = requests.post(NTFY_URL, data=msg.encode("utf-8"), headers=headers, timeout=10)
        if r.status_code == 200:
            log.info("Push notification sent successfully.")
        else:
            log.warning(f"Push notification failed: {r.status_code}")
    except Exception as e:
        log.error(f"Push notification error: {e}")

# ─── Dashboard HTML Generator ──────────────────────────────────────────────────
def generate_html(arbs: list, evs: list, state: dict) -> str:
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    generated_at = ist_now.strftime("%d %b %Y, %I:%M:%S %p IST")
    pass_hash_js = hashlib.sha256(DASHBOARD_PASS.encode()).hexdigest()

    arbs_json = json.dumps(arbs, ensure_ascii=False)
    evs_json  = json.dumps(evs,  ensure_ascii=False)

    sports_set = sorted(set(x["sport"] for x in arbs + evs))
    books_set  = sorted(set(
        o["book"] for x in arbs for o in x.get("outcomes", [])
    ) | set(x["book"] for x in evs))

    sports_checkboxes = "\n".join(
        f'<label class="filter-cb"><input type="checkbox" checked value="{s}" data-filter="sport"> <span>{s.replace("_"," ").title()}</span></label>'
        for s in sports_set
    )
    books_checkboxes = "\n".join(
        f'<label class="filter-cb"><input type="checkbox" checked value="{b}" data-filter="book"> <span>{b}</span></label>'
        for b in books_set
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Arb Sniper ⚡ Dashboard</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
<style>
  :root {{
    --bg0:#09090b; --bg1:#111113; --bg2:#18181b; --bg3:#27272a;
    --border:#3f3f46; --accent:#22d3ee; --accent2:#a78bfa;
    --green:#4ade80; --red:#f87171; --yellow:#fbbf24;
    --txt:#e4e4e7; --txt2:#a1a1aa; --txt3:#71717a;
    --font:'JetBrains Mono',monospace;
  }}
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Syne:wght@400;700;800&display=swap');
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg0);color:var(--txt);font-family:var(--font);min-height:100vh;overflow-x:hidden}}

  /* ── LOCKSCREEN ── */
  #lockscreen{{
    position:fixed;inset:0;z-index:9999;background:var(--bg0);
    display:flex;align-items:center;justify-content:center;
    flex-direction:column;gap:20px;
  }}
  .lock-box{{
    background:var(--bg2);border:1px solid var(--border);border-radius:16px;
    padding:40px 48px;display:flex;flex-direction:column;align-items:center;gap:20px;
    box-shadow:0 0 80px rgba(34,211,238,0.08);
  }}
  .lock-icon{{font-size:48px;color:var(--accent);animation:pulse 2s infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}
  .lock-title{{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:var(--txt);letter-spacing:2px}}
  .lock-sub{{color:var(--txt3);font-size:12px;letter-spacing:1px}}
  #lock-input{{
    background:var(--bg3);border:1px solid var(--border);border-radius:8px;
    color:var(--txt);font-family:var(--font);font-size:16px;padding:12px 20px;
    width:260px;outline:none;text-align:center;letter-spacing:4px;
    transition:border-color .2s;
  }}
  #lock-input:focus{{border-color:var(--accent)}}
  #lock-btn{{
    background:var(--accent);color:#000;font-family:'Syne',sans-serif;font-weight:700;
    border:none;border-radius:8px;padding:12px 40px;font-size:14px;cursor:pointer;
    letter-spacing:1px;transition:opacity .2s;width:100%;
  }}
  #lock-btn:hover{{opacity:.85}}
  #lock-err{{color:var(--red);font-size:12px;display:none}}

  /* ── MAIN APP ── */
  #app{{display:none}}
  .topbar{{
    background:var(--bg1);border-bottom:1px solid var(--border);
    padding:0 28px;height:56px;display:flex;align-items:center;
    justify-content:space-between;position:sticky;top:0;z-index:100;
  }}
  .logo{{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;letter-spacing:2px;
    background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;
    -webkit-text-fill-color:transparent;
  }}
  .topbar-right{{display:flex;align-items:center;gap:16px;font-size:12px;color:var(--txt3)}}
  .stat-pill{{
    background:var(--bg3);border:1px solid var(--border);border-radius:20px;
    padding:4px 12px;font-size:11px;display:flex;align-items:center;gap:6px;
  }}
  .dot{{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 1.5s infinite}}
  @keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}

  .hero{{
    padding:32px 28px 16px;display:grid;
    grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;
  }}
  .hero-card{{
    background:var(--bg2);border:1px solid var(--border);border-radius:12px;
    padding:20px;position:relative;overflow:hidden;
  }}
  .hero-card::before{{
    content:'';position:absolute;top:0;left:0;right:0;height:2px;
    background:linear-gradient(90deg,var(--accent),var(--accent2));
  }}
  .hero-label{{font-size:11px;color:var(--txt3);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px}}
  .hero-val{{font-size:28px;font-weight:700;font-family:'Syne',sans-serif}}
  .hero-sub{{font-size:11px;color:var(--txt3);margin-top:4px}}
  .green{{color:var(--green)}} .red{{color:var(--red)}} .yellow{{color:var(--yellow)}} .cyan{{color:var(--accent)}}

  .section-header{{
    padding:24px 28px 12px;display:flex;align-items:center;
    justify-content:space-between;
  }}
  .section-title{{
    font-family:'Syne',sans-serif;font-size:16px;font-weight:700;
    display:flex;align-items:center;gap:10px;letter-spacing:1px;
  }}
  .badge{{
    background:var(--accent);color:#000;font-size:10px;font-weight:700;
    border-radius:10px;padding:2px 8px;
  }}
  .filter-btn{{
    background:var(--bg3);border:1px solid var(--border);border-radius:8px;
    color:var(--txt2);font-family:var(--font);font-size:12px;padding:6px 14px;
    cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .2s;
  }}
  .filter-btn:hover{{border-color:var(--accent);color:var(--accent)}}

  .cards-grid{{
    padding:0 28px 28px;display:grid;
    grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;
  }}

  .card{{
    background:var(--bg2);border:1px solid var(--border);border-radius:12px;
    padding:18px;transition:border-color .2s,transform .15s;position:relative;
  }}
  .card:hover{{border-color:var(--accent);transform:translateY(-2px)}}
  .card-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}}
  .card-type{{
    font-size:10px;letter-spacing:1.5px;text-transform:uppercase;font-weight:700;
    padding:3px 8px;border-radius:4px;
  }}
  .card-type.arb{{background:rgba(74,222,128,.12);color:var(--green);border:1px solid rgba(74,222,128,.3)}}
  .card-type.ev{{background:rgba(34,211,238,.12);color:var(--accent);border:1px solid rgba(34,211,238,.3)}}
  .card-profit{{
    font-family:'Syne',sans-serif;font-weight:800;font-size:20px;
  }}
  .card-profit.arb{{color:var(--green)}}
  .card-profit.ev{{color:var(--accent)}}
  .match-name{{font-size:13px;font-weight:500;margin-bottom:4px;color:var(--txt)}}
  .match-meta{{font-size:11px;color:var(--txt3);display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}}
  .meta-chip{{display:flex;align-items:center;gap:4px}}

  .outcomes-table{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:12px}}
  .outcomes-table th{{
    color:var(--txt3);text-transform:uppercase;font-size:10px;letter-spacing:1px;
    font-weight:500;text-align:left;padding:4px 6px;border-bottom:1px solid var(--border);
  }}
  .outcomes-table td{{padding:6px 6px;border-bottom:1px solid rgba(63,63,70,.4)}}
  .odds-val{{color:var(--yellow);font-weight:700}}
  .book-tag{{
    background:var(--bg3);border:1px solid var(--border);border-radius:4px;
    padding:2px 6px;font-size:10px;color:var(--txt2);
  }}
  .stake-exact{{color:var(--txt3);font-size:10px}}
  .stake-big{{color:var(--txt);font-weight:700;font-size:13px}}

  .card-footer{{display:flex;justify-content:space-between;align-items:center;margin-top:10px}}
  .calc-btn{{
    background:transparent;border:1px solid var(--accent2);color:var(--accent2);
    font-family:var(--font);font-size:11px;padding:5px 12px;border-radius:6px;
    cursor:pointer;transition:all .2s;display:flex;align-items:center;gap:5px;
  }}
  .calc-btn:hover{{background:var(--accent2);color:#000}}
  .profit-amt{{font-size:11px;color:var(--txt3)}}

  /* EV card specifics */
  .ev-row{{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid rgba(63,63,70,.4)}}
  .ev-label{{font-size:11px;color:var(--txt3)}}
  .ev-val{{font-size:12px;color:var(--txt)}}

  /* ── FILTER PANEL ── */
  .filter-overlay{{
    position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.6);
    backdrop-filter:blur(4px);display:none;
  }}
  .filter-panel{{
    position:fixed;right:0;top:0;bottom:0;width:320px;z-index:501;
    background:var(--bg1);border-left:1px solid var(--border);
    padding:28px;overflow-y:auto;transform:translateX(100%);
    transition:transform .3s ease;
  }}
  .filter-panel.open{{transform:translateX(0)}}
  .filter-overlay.open{{display:block}}
  .filter-title{{font-family:'Syne',sans-serif;font-size:16px;font-weight:700;margin-bottom:24px;display:flex;justify-content:space-between;align-items:center}}
  .close-btn{{background:none;border:none;color:var(--txt3);font-size:18px;cursor:pointer}}
  .filter-section{{margin-bottom:24px}}
  .filter-section-label{{font-size:11px;color:var(--txt3);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:12px}}
  .filter-cb{{display:flex;align-items:center;gap:8px;margin-bottom:8px;cursor:pointer;font-size:12px;color:var(--txt2)}}
  .filter-cb input{{accent-color:var(--accent);width:14px;height:14px}}
  .range-wrap{{display:flex;flex-direction:column;gap:8px}}
  .range-label{{display:flex;justify-content:space-between;font-size:12px;color:var(--txt2)}}
  input[type=range]{{
    width:100%;accent-color:var(--accent);background:var(--bg3);
    border-radius:4px;height:4px;
  }}
  .apply-btn{{
    width:100%;background:var(--accent);color:#000;font-family:'Syne',sans-serif;
    font-weight:700;border:none;border-radius:8px;padding:12px;font-size:14px;
    cursor:pointer;margin-top:16px;letter-spacing:1px;
  }}

  /* ── CALCULATOR MODAL ── */
  .modal-overlay{{
    position:fixed;inset:0;z-index:600;background:rgba(0,0,0,.7);
    backdrop-filter:blur(6px);display:none;align-items:center;justify-content:center;
  }}
  .modal-overlay.open{{display:flex}}
  .calc-modal{{
    background:var(--bg2);border:1px solid var(--border);border-radius:16px;
    padding:32px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto;
  }}
  .calc-modal-title{{font-family:'Syne',sans-serif;font-weight:800;font-size:18px;margin-bottom:24px;display:flex;justify-content:space-between;align-items:center}}
  .calc-tabs{{display:flex;gap:8px;margin-bottom:20px}}
  .calc-tab{{
    background:var(--bg3);border:1px solid var(--border);border-radius:6px;
    color:var(--txt2);font-family:var(--font);font-size:12px;padding:6px 16px;
    cursor:pointer;transition:all .2s;
  }}
  .calc-tab.active{{background:var(--accent);color:#000;border-color:var(--accent)}}
  .calc-input-group{{margin-bottom:14px}}
  .calc-input-label{{font-size:11px;color:var(--txt3);letter-spacing:1px;margin-bottom:5px}}
  .calc-input{{
    background:var(--bg3);border:1px solid var(--border);border-radius:6px;
    color:var(--txt);font-family:var(--font);font-size:13px;padding:9px 12px;
    width:100%;outline:none;transition:border-color .2s;
  }}
  .calc-input:focus{{border-color:var(--accent)}}
  .calc-result{{
    background:var(--bg0);border:1px solid var(--border);border-radius:8px;
    padding:16px;margin-top:16px;
  }}
  .calc-result-row{{display:flex;justify-content:space-between;padding:5px 0;font-size:13px;border-bottom:1px solid rgba(63,63,70,.3)}}
  .calc-result-row:last-child{{border-bottom:none;font-weight:700;color:var(--green)}}
  .run-calc-btn{{
    width:100%;background:var(--accent2);color:#fff;font-family:'Syne',sans-serif;
    font-weight:700;border:none;border-radius:8px;padding:11px;font-size:14px;
    cursor:pointer;margin-top:14px;
  }}

  /* ── FOOTER ── */
  .footer{{text-align:center;padding:32px;color:var(--txt3);font-size:11px;border-top:1px solid var(--border)}}

  /* ── TABS ── */
  .main-tabs{{padding:0 28px 0;display:flex;gap:4px;border-bottom:1px solid var(--border);margin-bottom:0}}
  .main-tab{{
    background:none;border:none;border-bottom:2px solid transparent;
    color:var(--txt3);font-family:var(--font);font-size:13px;padding:12px 20px;
    cursor:pointer;transition:all .2s;
  }}
  .main-tab.active{{color:var(--accent);border-bottom-color:var(--accent)}}
  .tab-content{{display:none}}
  .tab-content.active{{display:block}}

  .empty-state{{
    text-align:center;padding:64px 28px;color:var(--txt3);
  }}
  .empty-icon{{font-size:40px;margin-bottom:16px;opacity:.3}}
  .empty-msg{{font-size:14px}}

  @media(max-width:600px){{
    .hero{{padding:16px;gap:10px}}
    .cards-grid{{padding:0 12px 20px;grid-template-columns:1fr}}
    .topbar{{padding:0 16px}}
    .calc-modal{{padding:20px}}
  }}
</style>
</head>
<body>

<!-- ─── LOCKSCREEN ─────────────────────────────────────── -->
<div id="lockscreen">
  <div class="lock-box">
    <i class="fas fa-shield-halved lock-icon"></i>
    <div class="lock-title">ARB SNIPER</div>
    <div class="lock-sub">ENTER ACCESS CODE TO PROCEED</div>
    <input id="lock-input" type="password" placeholder="••••••••" autocomplete="off"/>
    <button id="lock-btn" onclick="checkPass()"><i class="fas fa-unlock-alt"></i> UNLOCK DASHBOARD</button>
    <div id="lock-err"><i class="fas fa-triangle-exclamation"></i> Invalid password. Try again.</div>
  </div>
</div>

<!-- ─── MAIN APP ───────────────────────────────────────── -->
<div id="app">
  <div class="topbar">
    <div class="logo"><i class="fas fa-crosshairs"></i> ARB SNIPER</div>
    <div class="topbar-right">
      <div class="stat-pill"><div class="dot"></div> LIVE</div>
      <div class="stat-pill"><i class="fas fa-clock"></i> {generated_at}</div>
      <div class="stat-pill"><i class="fas fa-database"></i> {state.get('remaining_requests','?')} req left</div>
      <button class="filter-btn" onclick="openFilter()"><i class="fas fa-sliders"></i> Filters</button>
    </div>
  </div>

  <div class="hero">
    <div class="hero-card">
      <div class="hero-label"><i class="fas fa-bolt"></i> Arb Opportunities</div>
      <div class="hero-val green" id="stat-arb">0</div>
      <div class="hero-sub">Profit above 0.1%</div>
    </div>
    <div class="hero-card">
      <div class="hero-label"><i class="fas fa-chart-line"></i> Value Bets (+EV)</div>
      <div class="hero-val cyan" id="stat-ev">0</div>
      <div class="hero-sub">Edge above 0.5%</div>
    </div>
    <div class="hero-card">
      <div class="hero-label"><i class="fas fa-trophy"></i> Top Arb Profit</div>
      <div class="hero-val yellow" id="stat-top-arb">—</div>
      <div class="hero-sub">Best single opportunity</div>
    </div>
    <div class="hero-card">
      <div class="hero-label"><i class="fas fa-fire"></i> Top EV Edge</div>
      <div class="hero-val cyan" id="stat-top-ev">—</div>
      <div class="hero-sub">Highest edge found</div>
    </div>
    <div class="hero-card">
      <div class="hero-label"><i class="fas fa-coins"></i> Max Profit / 1K</div>
      <div class="hero-val green" id="stat-max-profit">—</div>
      <div class="hero-sub">On ₹1,000 stake</div>
    </div>
  </div>

  <!-- Main Tabs -->
  <div class="main-tabs">
    <button class="main-tab active" onclick="switchTab('arb')"><i class="fas fa-percent"></i> Arbitrage</button>
    <button class="main-tab" onclick="switchTab('ev')"><i class="fas fa-chart-bar"></i> Value Bets (+EV)</button>
  </div>

  <!-- ARB TAB -->
  <div id="tab-arb" class="tab-content active">
    <div class="section-header">
      <div class="section-title"><i class="fas fa-bolt"></i> Arbitrage Bets <span class="badge" id="arb-count">0</span></div>
    </div>
    <div id="arb-cards" class="cards-grid"></div>
  </div>

  <!-- EV TAB -->
  <div id="tab-ev" class="tab-content">
    <div class="section-header">
      <div class="section-title"><i class="fas fa-chart-line"></i> Value Bets <span class="badge" id="ev-count">0</span></div>
    </div>
    <div id="ev-cards" class="cards-grid"></div>
  </div>

  <div class="footer">
    <i class="fas fa-shield-halved"></i> ARB SNIPER v3.0 &nbsp;·&nbsp; Data: The Odds API + BC.Game &nbsp;·&nbsp;
    Powered by Pinnacle True Odds &nbsp;·&nbsp; Generated: {generated_at}
    <br><br><span style="color:#52525b">For informational purposes only. Gamble responsibly.</span>
  </div>
</div>

<!-- ─── FILTER PANEL ───────────────────────────────────── -->
<div class="filter-overlay" id="filter-overlay" onclick="closeFilter()"></div>
<div class="filter-panel" id="filter-panel">
  <div class="filter-title">
    <span><i class="fas fa-sliders"></i> Filters</span>
    <button class="close-btn" onclick="closeFilter()"><i class="fas fa-xmark"></i></button>
  </div>
  <div class="filter-section">
    <div class="filter-section-label">Minimum Profit / Edge %</div>
    <div class="range-wrap">
      <div class="range-label"><span>0%</span><span id="range-val">0.1%</span><span>10%</span></div>
      <input type="range" id="min-profit-range" min="0" max="10" step="0.1" value="0.1"
        oninput="document.getElementById('range-val').textContent=this.value+'%'"/>
    </div>
  </div>
  <div class="filter-section">
    <div class="filter-section-label">Sports</div>
    {sports_checkboxes}
  </div>
  <div class="filter-section">
    <div class="filter-section-label">Bookmakers</div>
    {books_checkboxes}
  </div>
  <button class="apply-btn" onclick="applyFilters()"><i class="fas fa-check"></i> Apply Filters</button>
</div>

<!-- ─── CALCULATOR MODAL ───────────────────────────────── -->
<div class="modal-overlay" id="calc-modal">
  <div class="calc-modal">
    <div class="calc-modal-title">
      <span><i class="fas fa-calculator"></i> Arb Calculator</span>
      <button class="close-btn" onclick="closeCalc()"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="calc-tabs">
      <button class="calc-tab active" onclick="switchCalcTab('2way')">2-Way</button>
      <button class="calc-tab" onclick="switchCalcTab('3way')">3-Way</button>
    </div>
    <div id="calc-2way">
      <div class="calc-input-group"><div class="calc-input-label">ODDS — OUTCOME 1</div><input class="calc-input" id="c2-o1" placeholder="e.g. 2.15" type="number" step="0.01"/></div>
      <div class="calc-input-group"><div class="calc-input-label">ODDS — OUTCOME 2</div><input class="calc-input" id="c2-o2" placeholder="e.g. 2.05" type="number" step="0.01"/></div>
      <div class="calc-input-group"><div class="calc-input-label">TOTAL STAKE (₹)</div><input class="calc-input" id="c2-stake" placeholder="e.g. 10000" type="number" value="10000"/></div>
      <button class="run-calc-btn" onclick="runCalc(2)"><i class="fas fa-play"></i> Calculate</button>
    </div>
    <div id="calc-3way" style="display:none">
      <div class="calc-input-group"><div class="calc-input-label">ODDS — OUTCOME 1 (HOME)</div><input class="calc-input" id="c3-o1" placeholder="e.g. 2.50" type="number" step="0.01"/></div>
      <div class="calc-input-group"><div class="calc-input-label">ODDS — OUTCOME 2 (DRAW)</div><input class="calc-input" id="c3-o2" placeholder="e.g. 3.20" type="number" step="0.01"/></div>
      <div class="calc-input-group"><div class="calc-input-label">ODDS — OUTCOME 3 (AWAY)</div><input class="calc-input" id="c3-o3" placeholder="e.g. 2.80" type="number" step="0.01"/></div>
      <div class="calc-input-group"><div class="calc-input-label">TOTAL STAKE (₹)</div><input class="calc-input" id="c3-stake" placeholder="e.g. 10000" type="number" value="10000"/></div>
      <button class="run-calc-btn" onclick="runCalc(3)"><i class="fas fa-play"></i> Calculate</button>
    </div>
    <div id="calc-result-box" class="calc-result" style="display:none"></div>
  </div>
</div>

<script>
// ─── DATA ────────────────────────────────────────────────
const ALL_ARBS = {arbs_json};
const ALL_EVS  = {evs_json};
let filteredArbs = [...ALL_ARBS];
let filteredEvs  = [...ALL_EVS];

// ─── LOCKSCREEN ──────────────────────────────────────────
const PASS_HASH = "{pass_hash_js}";
function checkPass() {{
  const val = document.getElementById('lock-input').value;
  const hash = CryptoJS.SHA256(val).toString();
  if (hash === PASS_HASH) {{
    document.getElementById('lockscreen').style.display='none';
    document.getElementById('app').style.display='block';
    renderAll();
  }} else {{
    document.getElementById('lock-err').style.display='block';
    document.getElementById('lock-input').value='';
    document.getElementById('lock-input').style.borderColor='var(--red)';
    setTimeout(()=>{{document.getElementById('lock-input').style.borderColor=''}},1500);
  }}
}}
document.getElementById('lock-input').addEventListener('keydown',e=>{{if(e.key==='Enter')checkPass()}});

// ─── TABS ─────────────────────────────────────────────────
function switchTab(tab) {{
  document.querySelectorAll('.main-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+tab).classList.add('active');
  event.target.classList.add('active');
}}

// ─── RENDER ───────────────────────────────────────────────
function sportIcon(sport) {{
  const s = sport.toLowerCase();
  if(s.includes('soccer')||s.includes('football')) return 'fa-futbol';
  if(s.includes('basket')) return 'fa-basketball';
  if(s.includes('hockey')) return 'fa-hockey-puck';
  if(s.includes('tennis')) return 'fa-table-tennis-paddle-ball';
  if(s.includes('mma')||s.includes('boxing')) return 'fa-hand-fist';
  if(s.includes('cricket')) return 'fa-cricket-bat-ball';
  if(s.includes('baseball')) return 'fa-baseball';
  if(s.includes('golf')) return 'fa-golf-ball-tee';
  if(s.includes('rugby')) return 'fa-football';
  return 'fa-trophy';
}}
function fmtDate(d) {{
  if(!d) return '—';
  try {{return new Date(d).toLocaleString('en-IN',{{timeZone:'Asia/Kolkata',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}})}} catch(e){{return d}}
}}
function bookLogo(key) {{
  const map={{'pinnacle':'PIN','bet365':'B365','betway':'BW','draftkings':'DK',
    'fanduel':'FD','betmgm':'MGM','unibet':'UNI','stake':'STK',
    'marathonbet':'MAR','parimatch':'PAR','betfair':'BF','dafabet':'DAF',
    'bovada':'BOV','onexbet':'1XB','bcgame':'BCG'}};
  return map[key]||key.toUpperCase().slice(0,4);
}}

function renderArbs(data) {{
  const el = document.getElementById('arb-cards');
  document.getElementById('arb-count').textContent = data.length;
  if(data.length===0) {{
    el.innerHTML='<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon"><i class="fas fa-magnifying-glass"></i></div><div class="empty-msg">No arbitrage opportunities match current filters.</div></div>';
    return;
  }}
  el.innerHTML = data.map((a,i)=>{{
    const outRows = a.outcomes.map(o=>`
      <tr>
        <td><span class="book-tag">${{bookLogo(o.book_key)}}</span> ${{o.name}}</td>
        <td><span class="odds-val">${{o.odds}}</span></td>
        <td>
          <div class="stake-exact">Exact: ₹${{o.stake}}</div>
          <div class="stake-big">₹${{o.stake_rounded}}</div>
        </td>
      </tr>`).join('');
    const oddsForCalc = a.outcomes.map(o=>o.odds).join(',');
    return `
    <div class="card" id="arb-card-${{i}}" data-sport="${{a.sport}}" data-books="${{a.outcomes.map(o=>o.book).join(',')}}">
      <div class="card-header">
        <span class="card-type arb"><i class="fas fa-percent"></i> ${{a.ways}}-WAY ARB</span>
        <span class="card-profit arb">+${{a.profit_pct}}%</span>
      </div>
      <div class="match-name"><i class="fas ${{sportIcon(a.sport)}}"></i> ${{a.match}}</div>
      <div class="match-meta">
        <span class="meta-chip"><i class="fas fa-calendar"></i> ${{fmtDate(a.commence)}}</span>
        <span class="meta-chip"><i class="fas fa-tag"></i> ${{a.market}}</span>
        <span class="meta-chip"><i class="fas fa-percent"></i> Impl: ${{(a.total_implied*100).toFixed(2)}}%</span>
      </div>
      <table class="outcomes-table">
        <thead><tr><th>Outcome / Book</th><th>Odds</th><th>Stake (₹1000)</th></tr></thead>
        <tbody>${{outRows}}</tbody>
      </table>
      <div class="card-footer">
        <span class="profit-amt"><i class="fas fa-coins"></i> Profit: ₹${{a.profit_amt}} on ₹1000</span>
        <button class="calc-btn" onclick="openCalcWith(${{JSON.stringify(oddsForCalc)}},${{a.ways}})">
          <i class="fas fa-calculator"></i> Calc
        </button>
      </div>
    </div>`;
  }}).join('');
}}

function renderEvs(data) {{
  const el = document.getElementById('ev-cards');
  document.getElementById('ev-count').textContent = data.length;
  if(data.length===0) {{
    el.innerHTML='<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon"><i class="fas fa-chart-line"></i></div><div class="empty-msg">No value bets match current filters.</div></div>';
    return;
  }}
  el.innerHTML = data.map((v,i)=>{{
    return `
    <div class="card" id="ev-card-${{i}}" data-sport="${{v.sport}}" data-books="${{v.book}}">
      <div class="card-header">
        <span class="card-type ev"><i class="fas fa-chart-line"></i> +EV BET</span>
        <span class="card-profit ev">+${{v.edge_pct}}%</span>
      </div>
      <div class="match-name"><i class="fas ${{sportIcon(v.sport)}}"></i> ${{v.match}}</div>
      <div class="match-meta">
        <span class="meta-chip"><i class="fas fa-calendar"></i> ${{fmtDate(v.commence)}}</span>
        <span class="meta-chip"><i class="fas fa-tag"></i> ${{v.market}}</span>
      </div>
      <div class="ev-row"><span class="ev-label"><i class="fas fa-bullseye"></i> Outcome</span><span class="ev-val">${{v.outcome}}</span></div>
      <div class="ev-row"><span class="ev-label"><i class="fas fa-building-columns"></i> Bookmaker</span><span class="ev-val"><span class="book-tag">${{bookLogo(v.book_key)}}</span> ${{v.book}}</span></div>
      <div class="ev-row"><span class="ev-label"><i class="fas fa-tag"></i> Offered Odds</span><span class="odds-val">${{v.offered_odds}}</span></div>
      <div class="ev-row"><span class="ev-label"><i class="fas fa-crosshairs"></i> True Odds (Pinnacle)</span><span class="ev-val">${{v.true_odds}}</span></div>
      <div class="ev-row"><span class="ev-label"><i class="fas fa-percent"></i> True Probability</span><span class="ev-val">${{v.true_prob_pct}}%</span></div>
      <div class="ev-row" style="border:none">
        <span class="ev-label"><i class="fas fa-coins"></i> Kelly Stake (30%)</span>
        <span class="ev-val">
          <span class="stake-exact">Exact: ₹${{v.kelly_stake}}</span>
          <span class="stake-big" style="margin-left:8px">₹${{v.kelly_stake_rounded}}</span>
        </span>
      </div>
    </div>`;
  }}).join('');
}}

function updateStats(arbs, evs) {{
  document.getElementById('stat-arb').textContent  = arbs.length;
  document.getElementById('stat-ev').textContent   = evs.length;
  document.getElementById('stat-top-arb').textContent = arbs.length ? '+'+arbs[0].profit_pct+'%' : '—';
  document.getElementById('stat-top-ev').textContent  = evs.length  ? '+'+evs[0].edge_pct+'%'   : '—';
  document.getElementById('stat-max-profit').textContent = arbs.length ? '₹'+arbs[0].profit_amt : '—';
}}

function renderAll() {{
  renderArbs(filteredArbs);
  renderEvs(filteredEvs);
  updateStats(filteredArbs, filteredEvs);
}}

// ─── FILTERS ──────────────────────────────────────────────
function openFilter()  {{ document.getElementById('filter-panel').classList.add('open');  document.getElementById('filter-overlay').classList.add('open'); }}
function closeFilter() {{ document.getElementById('filter-panel').classList.remove('open'); document.getElementById('filter-overlay').classList.remove('open'); }}

function applyFilters() {{
  const minPct   = parseFloat(document.getElementById('min-profit-range').value) || 0;
  const sports   = [...document.querySelectorAll('[data-filter="sport"]:checked')].map(e=>e.value);
  const books    = [...document.querySelectorAll('[data-filter="book"]:checked')].map(e=>e.value);

  filteredArbs = ALL_ARBS.filter(a => {{
    if(a.profit_pct < minPct) return false;
    if(!sports.includes(a.sport)) return false;
    const arbBooks = a.outcomes.map(o=>o.book);
    return arbBooks.some(b=>books.includes(b));
  }});
  filteredEvs = ALL_EVS.filter(v => {{
    if(v.edge_pct < minPct) return false;
    if(!sports.includes(v.sport)) return false;
    return books.includes(v.book);
  }});
  closeFilter();
  renderAll();
}}

// ─── CALCULATOR ───────────────────────────────────────────
function openCalcWith(oddsStr, ways) {{
  const odds = oddsStr.split(',').map(Number);
  if(ways===2) {{
    document.getElementById('c2-o1').value = odds[0]||'';
    document.getElementById('c2-o2').value = odds[1]||'';
    switchCalcTab('2way');
  }} else {{
    document.getElementById('c3-o1').value = odds[0]||'';
    document.getElementById('c3-o2').value = odds[1]||'';
    document.getElementById('c3-o3').value = odds[2]||'';
    switchCalcTab('3way');
  }}
  document.getElementById('calc-result-box').style.display='none';
  document.getElementById('calc-modal').classList.add('open');
}}
function closeCalc() {{ document.getElementById('calc-modal').classList.remove('open'); }}
function switchCalcTab(t) {{
  document.querySelectorAll('.calc-tab').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('calc-2way').style.display = t==='2way'?'block':'none';
  document.getElementById('calc-3way').style.display = t==='3way'?'block':'none';
  document.getElementById('calc-result-box').style.display='none';
}}
function runCalc(ways) {{
  let odds=[], stake=0;
  if(ways===2) {{
    odds=[parseFloat(document.getElementById('c2-o1').value)||0,parseFloat(document.getElementById('c2-o2').value)||0];
    stake=parseFloat(document.getElementById('c2-stake').value)||10000;
  }} else {{
    odds=[parseFloat(document.getElementById('c3-o1').value)||0,parseFloat(document.getElementById('c3-o2').value)||0,parseFloat(document.getElementById('c3-o3').value)||0];
    stake=parseFloat(document.getElementById('c3-stake').value)||10000;
  }}
  if(odds.some(o=>o<=1)) {{ alert('Please enter valid odds > 1'); return; }}
  const totalImpl = odds.reduce((s,o)=>s+1/o,0);
  const profitPct = (1/totalImpl - 1)*100;
  const stakes = odds.map(o=>(1/o)/totalImpl*stake);
  const profit = stake*(1/totalImpl-1);
  const rb = document.getElementById('calc-result-box');
  let rows = odds.map((o,i)=>`<div class="calc-result-row"><span>Stake on Outcome ${{i+1}} (@ ${{o}})</span><span>₹${{stakes[i].toFixed(2)}}</span></div>`).join('');
  rb.innerHTML = rows +
    `<div class="calc-result-row"><span>Total Stake</span><span>₹${{stake.toFixed(2)}}</span></div>`+
    `<div class="calc-result-row"><span>Total Implied %</span><span>${{(totalImpl*100).toFixed(3)}}%</span></div>`+
    (profitPct>0
      ? `<div class="calc-result-row"><span>ARB PROFIT</span><span style="color:var(--green)">+₹${{profit.toFixed(2)}} (+${{profitPct.toFixed(3)}}%)</span></div>`
      : `<div class="calc-result-row"><span>RESULT</span><span style="color:var(--red)">NO ARB — Margin: ${{Math.abs(profitPct).toFixed(3)}}%</span></div>`);
  rb.style.display='block';
}}
</script>
</body>
</html>"""
    return html

# ─── Main Orchestrator ─────────────────────────────────────────────────────────
def main():
    log.info("╔══ ARB SNIPER v3.0 — Starting Run ══╗")
    state = load_state()
    log.info(f"API State loaded. Remaining: {state['remaining_requests']} requests")

    # Fetch Odds API data concurrently
    odds_events = fetch_all_odds(state)
    log.info(f"Total events from Odds API: {len(odds_events)}")

    # Fetch & merge BC.Game data
    bc_events   = fetch_bcgame_events()
    all_events  = merge_bcgame(odds_events, bc_events)
    log.info(f"Total combined events: {len(all_events)}")

    # Save updated state
    save_state(state)

    # Quant scanning
    arbs = scan_arbitrage(all_events)
    evs  = scan_ev_bets(all_events)

    # Push notification
    send_push(arbs, evs)

    # Generate dashboard
    html = generate_html(arbs, evs, state)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Dashboard written to {OUTPUT_HTML}")

    # Summary
    log.info(f"╠══ RESULTS ══╣")
    log.info(f"  Arbitrage opportunities: {len(arbs)}")
    if arbs:
        log.info(f"  Top Arb: {arbs[0]['match']} — {arbs[0]['profit_pct']}% profit")
    log.info(f"  Value bets (+EV):        {len(evs)}")
    if evs:
        log.info(f"  Top EV:  {evs[0]['match']} — {evs[0]['edge_pct']}% edge @ {evs[0]['book']}")
    log.info(f"  API requests remaining:  {state['remaining_requests']}")
    log.info("╚══ Run Complete ══╝")

if __name__ == "__main__":
    main()
