import os

def generate_interactive_dashboard():
    # We are writing a complete HTML/JS Web Application into the index.html file
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Arb Sniper Interactive Terminal</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0d1117; color: #c9d1d9; margin: 0; padding: 15px; max-width: 900px; margin: auto; }
            h1 { color: #58a6ff; text-align: center; font-size: 26px; margin-bottom: 20px; }
            
            /* Control Panel styling */
            .control-panel { background-color: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; margin-bottom: 20px; }
            .control-group { margin-bottom: 12px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; }
            label { font-weight: bold; color: #8b949e; margin-right: 10px; }
            input[type="text"], input[type="number"] { background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 8px; border-radius: 4px; width: 60%; font-weight: bold;}
            
            .sports-group { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 15px; }
            .sports-group label { background-color: #21262d; border: 1px solid #30363d; padding: 6px 10px; border-radius: 4px; cursor: pointer; color:#c9d1d9; font-weight:normal;}
            .sports-group input[type="checkbox"] { margin-right: 5px; }

            /* Button styling */
            .btn-group { display: flex; gap: 10px; }
            .btn { flex: 1; padding: 12px; font-size: 16px; font-weight: bold; border: none; border-radius: 6px; cursor: pointer; color: white; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            .btn-start { background-color: #238636; }
            .btn-start:active { background-color: #2ea043; }
            .btn-stop { background-color: #da3633; }
            .btn-stop:disabled { background-color: #444; color: #888; cursor: not-allowed;}

            /* Status & Telemetry */
            .status-bar { text-align: center; padding: 10px; margin-top: 10px; font-weight: bold; }
            .telemetry-banner { background-color: #1f6feb; color: white; padding: 10px; border-radius: 6px; text-align: center; margin-bottom: 20px; font-weight: bold; }

            /* Tabs */
            .tabs { display: flex; border-bottom: 1px solid #30363d; margin-bottom: 20px; margin-top:20px;}
            .tab { flex: 1; text-align: center; padding: 12px; cursor: pointer; font-size: 16px; font-weight: bold; color: #8b949e; }
            .tab.active { color: #ffffff; border-bottom: 3px solid #58a6ff; background-color: #161b22; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }

            /* Cards */
            .card { background-color: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; margin-bottom: 20px; font-size: 15px; line-height: 1.6; }
            .card-header { font-weight: bold; font-size: 16px; margin-bottom: 12px; border-bottom: 1px dashed #30363d; padding-bottom: 8px; color: #58a6ff; }
            .detail-block { margin-bottom: 12px; }
            .highlight { color: #ffffff; font-weight: bold; }
            .highlight-stake { color: #e3b341; font-weight: bold; }
            .profit-highlight { color: #3fb950; font-weight: bold; font-size: 18px; }
            .loss-highlight { color: #ff7b72; font-weight: bold; font-size: 18px; }
            .empty-state { text-align: center; color: #8b949e; padding: 30px; font-style: italic; background-color: #161b22; border-radius: 8px; border: 1px dashed #30363d; }
            
            /* Calculator Specifics */
            .calc-box { background-color: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 15px; margin-top: 15px; }
            .calc-input-group { display: flex; gap: 15px; margin-bottom: 15px; }
            .calc-input-group div { flex: 1; }
            .calc-input-group label { display: block; margin-bottom: 5px; font-size: 13px; }
            .calc-input-group input { width: 100%; box-sizing: border-box; font-size: 16px; }
        </style>
    </head>
    <body onload="runCalc()">
        <h1>📡 Arb Sniper Terminal</h1>

        <div id="telemetryBanner" class="telemetry-banner" style="display:none;">
            API Credits Remaining: <span id="apiQuota">Checking...</span>
        </div>

        <div class="control-panel">
            <div class="control-group">
                <label for="apiKey">API Key:</label>
                <input type="text" id="apiKey" placeholder="Paste your API Key here">
            </div>
            <div class="control-group">
                <label for="bankroll">Total Bankroll (₹):</label>
                <input type="number" id="bankroll" value="1500" oninput="syncBankroll()">
            </div>
            <div class="control-group">
                <label for="evThreshold">Min EV %:</label>
                <input type="number" id="evThreshold" step="0.1" value="1.5">
            </div>
            <div class="control-group">
                <label for="arbThreshold">Min Arb %:</label>
                <input type="number" id="arbThreshold" step="0.1" value="1.0">
            </div>
            
            <label>Target Sports:</label>
            <div class="sports-group">
                <label><input type="checkbox" value="soccer_epl" checked> EPL</label>
                <label><input type="checkbox" value="soccer_uefa_champs_league" checked> UEFA</label>
                <label><input type="checkbox" value="basketball_nba" checked> NBA</label>
                <label><input type="checkbox" value="icehockey_nhl" checked> NHL</label>
                <label><input type="checkbox" value="tennis_atp" checked> ATP</label>
                <label><input type="checkbox" value="tennis_wta" checked> WTA</label>
            </div>

            <div class="btn-group">
                <button id="startBtn" class="btn btn-start" onclick="startScanning()">▶️ Start Sweep</button>
                <button id="stopBtn" class="btn btn-stop" onclick="stopScanning()" disabled>⏸️ Pause</button>
            </div>
            <div id="statusText" class="status-bar">System Ready.</div>
        </div>

        <div class="tabs">
            <div class="tab active" id="tab-ev" onclick="switchTab('ev')">💎 EV (<span id="evCount">0</span>)</div>
            <div class="tab" id="tab-arb" onclick="switchTab('arb')">🏆 Arb (<span id="arbCount">0</span>)</div>
            <div class="tab" id="tab-calc" onclick="switchTab('calc')">🧮 Calc</div>
        </div>

        <div id="content-ev" class="tab-content active">
            <div id="evResults" class="empty-state">✅ Press Start Sweep to scan the markets.</div>
        </div>
        
        <div id="content-arb" class="tab-content">
            <div id="arbResults" class="empty-state">✅ Press Start Sweep to scan the markets.</div>
        </div>

        <div id="content-calc" class="tab-content">
            <div class="card">
                <div class="card-header">🧮 Live Surebet Hedge Calculator</div>
                <p style="color:#8b949e; font-size:13px; margin-top:0;">If odds shift, enter the new odds here to instantly balance your hedge.</p>
                
                <div class="control-group" style="margin-bottom:15px;">
                    <label>Total Investment (₹):</label>
                    <input type="number" id="calcBankroll" value="1500" oninput="runCalc()">
                </div>

                <div class="calc-input-group">
                    <div>
                        <label>🔵 Bookie 1 Odds:</label>
                        <input type="number" id="calcOdds1" step="0.01" value="2.05" oninput="runCalc()">
                    </div>
                    <div>
                        <label>🔴 Bookie 2 Odds:</label>
                        <input type="number" id="calcOdds2" step="0.01" value="2.10" oninput="runCalc()">
                    </div>
                </div>

                <div id="calcResults" class="calc-box">
                    </div>
            </div>
        </div>

        <script>
            let isScanning = false;
            let currentSportIndex = 0;
            let scanInterval;
            let myBookies = 'pinnacle,onexbet,marathonbet,dafabet,stake,betfair_ex_eu,betway';
            
            let globalEVs = [];
            let globalArbs = [];

            const bookieMap = {
                'onexbet': '1xBet/Melbet', 'pinnacle': 'Pinnacle', 'marathonbet': 'Marathonbet', 
                'dafabet': 'Dafabet', 'stake': 'Stake.com', 'betfair_ex_eu': 'Betfair Exchange', 'betway': 'Betway'
            };
            function getBookieName(key) { return bookieMap[key] || key; }

            function switchTab(tab) {
                document.getElementById('content-ev').classList.remove('active');
                document.getElementById('content-arb').classList.remove('active');
                document.getElementById('content-calc').classList.remove('active');
                
                document.getElementById('tab-ev').classList.remove('active');
                document.getElementById('tab-arb').classList.remove('active');
                document.getElementById('tab-calc').classList.remove('active');
                
                document.getElementById('content-' + tab).classList.add('active');
                document.getElementById('tab-' + tab).classList.add('active');
            }

            // Sync Main Bankroll with Calculator
            function syncBankroll() {
                document.getElementById('calcBankroll').value = document.getElementById('bankroll').value;
                runCalc();
            }

            // HEDGE CALCULATOR MATH
            function runCalc() {
                let total = parseFloat(document.getElementById('calcBankroll').value);
                let o1 = parseFloat(document.getElementById('calcOdds1').value);
                let o2 = parseFloat(document.getElementById('calcOdds2').value);
                
                if(!total || !o1 || !o2) return;

                let p1 = 1 / o1;
                let p2 = 1 / o2;
                let margin = p1 + p2;
                let arbPct = (1 - margin) * 100;

                let s1 = (total / margin) * p1;
                let s2 = (total / margin) * p2;
                let profit = (total / margin) - total;
                
                let arbStatus = margin < 1 
                    ? `<span style="color:#3fb950">Valid Arb (${arbPct.toFixed(2)}%)</span>` 
                    : `<span style="color:#ff7b72">Negative Arb (${arbPct.toFixed(2)}%)</span>`;
                
                let profitClass = profit >= 0 ? 'profit-highlight' : 'loss-highlight';

                let html = `
                    <div style="display:flex; justify-content:space-between; margin-bottom:10px;">
                        <span>🔵 Target Stake 1: <strong class="highlight-stake">₹${s1.toFixed(0)}</strong></span>
                        <span style="color:#8b949e">Payout: ₹${(s1*o1).toFixed(0)}</span>
                    </div>
                    <div style="display:flex; justify-content:space-between; margin-bottom:15px;">
                        <span>🔴 Target Stake 2: <strong class="highlight-stake">₹${s2.toFixed(0)}</strong></span>
                        <span style="color:#8b949e">Payout: ₹${(s2*o2).toFixed(0)}</span>
                    </div>
                    <hr style="border:0; border-top:1px dashed #30363d; margin-bottom:15px;">
                    <div style="text-align:center;">
                        <span style="font-size:14px; color:#8b949e">Market Status: ${arbStatus}</span><br>
                        <span style="font-size:14px;">Guaranteed Net Profit: </span><br>
                        <strong class="${profitClass}">₹${profit.toFixed(0)}</strong>
                    </div>
                `;
                document.getElementById('calcResults').innerHTML = html;
            }

            // --- SCANNING LOGIC ---
            function removeVig(odds1, odds2) {
                let imp1 = 1 / odds1; let imp2 = 1 / odds2;
                let margin = imp1 + imp2;
                return [(1 / (imp1 / margin)), (1 / (imp2 / margin))];
            }

            function calculateKelly(softOdds, trueOdds, bankroll) {
                let b = softOdds - 1.0; let p = 1.0 / trueOdds; let q = 1.0 - p;
                let safeKelly = ((b * p - q) / b) * 0.30;
                if (safeKelly <= 0) return 0;
                if (safeKelly > 0.05) safeKelly = 0.05;
                return Math.max(20, bankroll * safeKelly);
            }

            function startScanning() {
                const apiKey = document.getElementById('apiKey').value.trim();
                if (!apiKey) {
                    alert("Please enter an API Key to start.");
                    return;
                }
                
                isScanning = true;
                document.getElementById('startBtn').disabled = true;
                document.getElementById('stopBtn').disabled = false;
                document.getElementById('statusText').innerText = "🔄 Scanning in progress...";
                document.getElementById('statusText').style.color = "#3fb950";
                document.getElementById('telemetryBanner').style.display = "block";
                
                if(currentSportIndex === 0) {
                     globalEVs = [];
                     globalArbs = [];
                }
                
                scanNextSport();
            }

            function stopScanning() {
                isScanning = false;
                clearTimeout(scanInterval);
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').innerText = "▶️ Resume Scan";
                document.getElementById('stopBtn').disabled = true;
                document.getElementById('statusText').innerText = "⏸️ Scan Paused.";
                document.getElementById('statusText').style.color = "#ff7b72";
            }

            async function scanNextSport() {
                if (!isScanning) return;

                const sportsCheckboxes = document.querySelectorAll('.sports-group input[type="checkbox"]:checked');
                const targetSports = Array.from(sportsCheckboxes).map(cb => cb.value);

                if (targetSports.length === 0) {
                    alert("Please select at least one sport.");
                    stopScanning();
                    return;
                }

                if (currentSportIndex >= targetSports.length) {
                    currentSportIndex = 0;
                    document.getElementById('statusText').innerText = "✅ Sweep Complete. Waiting 60 seconds...";
                    renderResults(); 
                    // Wait 60 seconds before looping again to save credits
                    scanInterval = setTimeout(scanNextSport, 60000); 
                    return;
                }

                const sport = targetSports[currentSportIndex];
                const apiKey = document.getElementById('apiKey').value.trim();
                
                document.getElementById('statusText').innerText = `🔄 Fetching data for ${sport}...`;

                try {
                    const url = `https://api.the-odds-api.com/v4/sports/${sport}/odds/?apiKey=${apiKey}&regions=eu&bookmakers=${myBookies}&markets=totals,spreads&oddsFormat=decimal`;
                    const response = await fetch(url);
                    
                    const remaining = response.headers.get('x-requests-remaining');
                    if (remaining) document.getElementById('apiQuota').innerText = remaining;

                    if (response.status === 401 || response.status === 429) {
                         alert(`API Error ${response.status}: Check quota or key.`);
                         stopScanning();
                         return;
                    }
                    
                    if(response.ok) {
                        const events = await response.json();
                        processEvents(events, sport);
                    }
                } catch (error) {
                    console.error("Fetch Error:", error);
                }

                currentSportIndex++;
                if(isScanning) {
                     scanInterval = setTimeout(scanNextSport, 1500);
                }
            }

            function processEvents(events, sport) {
                const targetBookies = myBookies.split(',');
                const bankroll = parseFloat(document.getElementById('bankroll').value);
                const evThreshold = parseFloat(document.getElementById('evThreshold').value);
                const arbThreshold = parseFloat(document.getElementById('arbThreshold').value);

                events.forEach(event => {
                    let evLines = {}; let arbLines = {};
                    let matchName = `${event.home_team} vs ${event.away_team}`;
                    let matchTime = new Date(event.commence_time).toLocaleString(); 
                    
                    (event.bookmakers || []).forEach(bookie => {
                        let bName = bookie.key;
                        if (!targetBookies.includes(bName)) return;
                        
                        (bookie.markets || []).forEach(market => {
                            if (['totals', 'spreads'].includes(market.key)) {
                                let mType = market.key.toUpperCase();
                                market.outcomes.forEach(outcome => {
                                    let point = outcome.point || '0';
                                    let name = outcome.name;
                                    let price = outcome.price;
                                    if (bName === 'betfair_ex_eu') price = 1 + (price - 1) * 0.97;
                                    
                                    let lineKey = `${mType}_${point}`;
                                    
                                    if(!evLines[lineKey]) evLines[lineKey] = { pinnacle: {}, best_soft: {} };
                                    if (bName === 'pinnacle') {
                                        evLines[lineKey].pinnacle[name] = price;
                                    } else {
                                        if (!evLines[lineKey].best_soft[name] || price > evLines[lineKey].best_soft[name].price) {
                                            evLines[lineKey].best_soft[name] = { price: price, bookie: bName };
                                        }
                                    }
                                    
                                    if(!arbLines[lineKey]) arbLines[lineKey] = {};
                                    if(!arbLines[lineKey][name] || price > arbLines[lineKey][name].price) {
                                        arbLines[lineKey][name] = { price: price, bookie: bName };
                                    }
                                });
                            }
                        });
                    });

                    Object.keys(evLines).forEach(lineKey => {
                        let data = evLines[lineKey];
                        let pinnyKeys = Object.keys(data.pinnacle);
                        if (pinnyKeys.length === 2) {
                            let s1 = pinnyKeys[0]; let s2 = pinnyKeys[1];
                            let trueOddsArr = removeVig(data.pinnacle[s1], data.pinnacle[s2]);
                            
                            let sides = [ {name: s1, trueO: trueOddsArr[0]}, {name: s2, trueO: trueOddsArr[1]} ];
                            
                            sides.forEach(sideData => {
                                let softInfo = data.best_soft[sideData.name];
                                if (softInfo && softInfo.price > sideData.trueO) {
                                    let evPct = ((softInfo.price / sideData.trueO) - 1) * 100;
                                    if (evPct >= evThreshold) {
                                        let stake = calculateKelly(softInfo.price, sideData.trueO, bankroll);
                                        globalEVs.push({
                                            pct: evPct, match: matchName, time: matchTime, sport: sport,
                                            line: lineKey, selection: sideData.name, odds: softInfo.price,
                                            true: sideData.trueO, bookie: softInfo.bookie, stake: stake
                                        });
                                    }
                                }
                            });
                        }
                    });

                    Object.keys(arbLines).forEach(lineKey => {
                        let outcomes = arbLines[lineKey];
                        let outKeys = Object.keys(outcomes);
                        if (outKeys.length === 2) {
                            let k1 = outKeys[0]; let k2 = outKeys[1];
                            let margin = (1 / outcomes[k1].price) + (1 / outcomes[k2].price);
                            if (margin < 1.0) {
                                let arbPct = (1 - margin) * 100;
                                if (arbPct >= arbThreshold) {
                                    globalArbs.push({
                                        pct: arbPct, match: matchName, time: matchTime, sport: sport, line: lineKey,
                                        s1: k1, s1_data: outcomes[k1], s2: k2, s2_data: outcomes[k2],
                                        stk1: (bankroll / margin) / outcomes[k1].price,
                                        stk2: (bankroll / margin) / outcomes[k2].price,
                                        profit: (bankroll / margin) - bankroll
                                    });
                                }
                            }
                        }
                    });
                });
            }

            function renderResults() {
                globalEVs.sort((a, b) => b.pct - a.pct);
                globalArbs.sort((a, b) => b.pct - a.pct);
                
                document.getElementById('evCount').innerText = globalEVs.length;
                document.getElementById('arbCount').innerText = globalArbs.length;

                let evHtml = "";
                if(globalEVs.length === 0) {
                    evHtml = '<div class="empty-state">✅ No EV edges found in the last sweep.</div>';
                } else {
                    globalEVs.forEach(ev => {
                        let cleanSport = ev.sport.replace('_', ' ').toUpperCase();
                        evHtml += `
                        <div class="card ev">
                            <div class="card-header">💎 💰 📈 <span class="highlight">${ev.pct.toFixed(2)}% EV</span> | ${ev.match}</div>
                            <div class="detail-block">
                                🏆 ${cleanSport}<br>📅 ${ev.time}<br>📈 <span class="highlight">${ev.line}</span>
                            </div>
                            <div class="detail-block">
                                💰 BET EXACTLY: <span class="highlight-stake">₹${ev.stake.toFixed(0)}</span><br>
                                👉 <span class="highlight">${ev.selection.toUpperCase()} ${ev.line.split('_')[1] || ''} @ ${ev.odds.toFixed(2)}</span> on ${getBookieName(ev.bookie)}
                            </div>
                            <div>🧠 True Odds: ${ev.true.toFixed(2)}</div>
                        </div>`;
                    });
                }
                document.getElementById('evResults').innerHTML = evHtml;

                let arbHtml = "";
                if(globalArbs.length === 0) {
                    arbHtml = '<div class="empty-state">✅ No Arbitrage opportunities found.</div>';
                } else {
                    globalArbs.forEach(arb => {
                        let cleanSport = arb.sport.replace('_', ' ').toUpperCase();
                        // Pre-fill the calculator!
                        let clickAction = `document.getElementById('calcOdds1').value=${arb.s1_data.price.toFixed(2)}; document.getElementById('calcOdds2').value=${arb.s2_data.price.toFixed(2)}; switchTab('calc'); runCalc();`;
                        
                        arbHtml += `
                        <div class="card arb">
                            <div class="card-header" style="display:flex; justify-content:space-between;">
                                <span>💎 💰 🚨 <span class="highlight">${arb.pct.toFixed(2)}% ARB</span> | ${arb.match}</span>
                                <button onclick="${clickAction}" style="background:#1f6feb; color:white; border:none; border-radius:4px; padding:4px 8px; cursor:pointer;">🧮 Calc</button>
                            </div>
                            <div class="detail-block">
                                🏆 ${cleanSport}<br>📅 ${arb.time}<br>📈 <span class="highlight">${arb.line}</span>
                            </div>
                            <div class="detail-block">
                                🔵 <span class="highlight-stake">₹${arb.stk1.toFixed(0)}</span> on <span class="highlight">${arb.s1.toUpperCase()} @ ${arb.s1_data.price.toFixed(2)}</span> [${getBookieName(arb.s1_data.bookie)}]<br>
                                🔴 <span class="highlight-stake">₹${arb.stk2.toFixed(0)}</span> on <span class="highlight">${arb.s2.toUpperCase()} @ ${arb.s2_data.price.toFixed(2)}</span> [${getBookieName(arb.s2_data.bookie)}]
                            </div>
                            <div>✨ Profit: <span style="color:#3fb950; font-weight:bold; font-size:16px;">₹${arb.profit.toFixed(0)}</span></div>
                        </div>`;
                    });
                }
                document.getElementById('arbResults').innerHTML = arbHtml;
            }
        </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("🌐 Interactive Dashboard Generated")

if __name__ == "__main__":
    generate_interactive_dashboard()
