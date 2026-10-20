"""Local Deribit options analytics dashboard. Run: python server.py"""
import concurrent.futures, json, math, os, sqlite3, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "deribit_snapshots.sqlite3"
API = "https://www.deribit.com/api/v2/public/"
CACHE, TTL = {}, 60
AI_CACHE = {}

def log(level, message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {level:<5} {message}", flush=True)

def call(method, **params):
    url = API + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent":"deribit-gex-dashboard/1.0"})
    # The Codex/terminal environment can carry a dead localhost proxy.  API
    # traffic is made directly from this local Python process; the browser
    # only ever talks to 127.0.0.1.
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    log("FETCH", f"Deribit {method} ({urllib.parse.urlencode(params)})")
    try:
        with direct_opener.open(req, timeout=20) as r:
            data = json.load(r)
        log("OK", f"Deribit {method} received")
    except Exception as e:
        log("FAIL", f"Deribit {method}: {type(e).__name__}: {e}")
        raise
    if "error" in data: raise RuntimeError(data["error"].get("message", "Deribit error"))
    return data["result"]

def ai_summary(asset, summary, selected, rv, premium):
    """Optional backend-only summary. Cached for an hour to avoid repeat API spend."""
    key = (asset, datetime.now(timezone.utc).strftime("%Y-%m-%d-%H"))
    if key in AI_CACHE: return AI_CACHE[key]
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"status":"N/A — set OPENAI_API_KEY on the backend to enable a generated summary; no key is sent to the browser."}
    context={"asset":asset,"net_gamma_usd_per_1pct":summary["netGamma"],"call_gamma":summary["callGamma"],"put_gamma":summary["putGamma"],"bias":summary["bias"],"surface_approx_30d":selected,"realized_vol_7d":rv,"iv_minus_rv":premium}
    body=json.dumps({"model":os.environ.get("OPENAI_MODEL","gpt-4o-mini"),"messages":[{"role":"system","content":"Kamu adalah analis derivatives crypto. Tulis tepat satu paragraf bahasa Indonesia santai, maksimal 4 kalimat, berdasarkan JSON. Jelaskan faktual dan netral; jangan memberi rekomendasi beli/jual."},{"role":"user","content":json.dumps(context)}],"temperature":0.2,"max_tokens":180}).encode()
    try:
        req=urllib.request.Request("https://api.openai.com/v1/chat/completions",data=body,headers={"Authorization":"Bearer "+api_key,"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=25) as r: text=json.load(r)["choices"][0]["message"]["content"].strip()
        result={"status":text}; AI_CACHE[key]=result; return result
    except Exception as e:
        return {"status":"N/A — AI summary request failed; market calculations are unaffected."}

def n_pdf(x): return math.exp(-x*x/2) / math.sqrt(2*math.pi)
def n_cdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def greeks(s, k, t, vol, option_type):
    if min(s,k,t,vol) <= 0: return {x:0 for x in ("delta","gamma","vega","theta","vanna","volga","charm")}
    root = math.sqrt(t); d1 = (math.log(s/k) + .5*vol*vol*t) / (vol*root); d2 = d1-vol*root; pdf=n_pdf(d1)
    delta = n_cdf(d1) if option_type == "call" else n_cdf(d1)-1
    gamma = pdf/(s*vol*root); vega=s*pdf*root
    theta = -(s*pdf*vol)/(2*root)  # r=0, annual unit
    vanna = -pdf*d2/vol
    volga = vega*d1*d2/vol
    # Calendar-time delta decay (r=0 crypto assumption); annual unit.
    charm = -pdf*d2/(2*t)
    return {"delta":delta,"gamma":gamma,"vega":vega,"theta":theta,"vanna":vanna,"volga":volga,"charm":charm}

def days(ts): return max(0, (ts/1000-time.time())/86400)
def nearest(items, predicate):
    choices=[x for x in items if predicate(x)]
    return min(choices, key=lambda x:abs(abs(x["delta"])-.25)) if choices else None
def pct(x): return round(x*100, 3) if x is not None else None

def realized_vol(asset):
    end=int(time.time()*1000); start=end-10*86400000
    chart=call("get_tradingview_chart_data", instrument_name=f"{asset}-PERPETUAL", start_timestamp=start, end_timestamp=end, resolution="60")
    close=chart.get("close", [])
    # sample one close per UTC day, then annualize standard deviation of log daily returns
    daily=close[::24] if len(close) >= 48 else close
    rets=[math.log(daily[i]/daily[i-1]) for i in range(1,len(daily)) if daily[i-1]>0 and daily[i]>0]
    if len(rets)<2: return None
    return math.sqrt(365)*math.sqrt(sum((x-sum(rets)/len(rets))**2 for x in rets)/(len(rets)-1))

def surface(rows, spot):
    by_exp={}
    for r in rows: by_exp.setdefault(r["expiry"], []).append(r)
    result=[]
    for exp, items in by_exp.items():
        atm=min(items,key=lambda x:abs(x["strike"]-spot))
        c=nearest(items,lambda x:x["type"]=="call"); p=nearest(items,lambda x:x["type"]=="put")
        if not(c and p and atm["iv"]): continue
        atmiv=atm["iv"]; rr=c["iv"]-p["iv"]; fly=(c["iv"]+p["iv"])/2-atmiv
        straddle_vega=2*atm.get("vega",0); strangle_vega=c.get("vega",0)+p.get("vega",0)
        result.append({"expiry":exp,"days":round(days(exp),1),"atmIv":pct(atmiv),"call25Iv":pct(c["iv"]),"put25Iv":pct(p["iv"]),"skew":round((p["iv"]-c["iv"])/atmiv,4),"rr":pct(rr),"fly":pct(fly),"atmStrike":atm["strike"],"callStrike":c["strike"],"putStrike":p["strike"],"c25Delta":round(c["delta"],4),"p25Delta":round(p["delta"],4),"hedgeRatio":round(straddle_vega/strangle_vega,4) if strangle_vega else None})
    return sorted(result,key=lambda x:x["days"])

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("CREATE TABLE IF NOT EXISTS snapshots(asset TEXT, day TEXT, payload TEXT, PRIMARY KEY(asset,day))")
        c.execute("CREATE TABLE IF NOT EXISTS realized_vol_history(asset TEXT, day TEXT, rv REAL, PRIMARY KEY(asset,day))")
        c.execute("CREATE TABLE IF NOT EXISTS spot_history(asset TEXT, day TEXT, spot REAL, PRIMARY KEY(asset,day))")
        c.execute("CREATE TABLE IF NOT EXISTS squeeze_snapshots(asset TEXT, captured_at TEXT, payload TEXT, PRIMARY KEY(asset,captured_at))")
        c.execute("CREATE TABLE IF NOT EXISTS oi_snapshots(asset TEXT, captured_at TEXT, payload TEXT, PRIMARY KEY(asset,captured_at))")
        # High-frequency, compact records used for directional context.  Unlike
        # the daily surface table, these deliberately retain intraday changes.
        c.execute("CREATE TABLE IF NOT EXISTS market_snapshots(asset TEXT, captured_at TEXT, payload TEXT, PRIMARY KEY(asset,captured_at))")
        # Regime records are deliberately separate from raw snapshots.  They
        # let the dashboard validate a score against the move that followed.
        c.execute("CREATE TABLE IF NOT EXISTS regime_snapshots(asset TEXT, captured_at TEXT, payload TEXT, forward_1h REAL, forward_4h REAL, forward_24h REAL, PRIMARY KEY(asset,captured_at))")
        c.execute("CREATE TABLE IF NOT EXISTS regime_state(asset TEXT PRIMARY KEY, stable_label TEXT, pending_label TEXT, pending_count INTEGER, updated_at TEXT)")

def bootstrap_realized_vol(asset):
    """One-time public Deribit RV backfill. It never claims to backfill implied IV."""
    with sqlite3.connect(DB) as c:
        existing=c.execute("SELECT COUNT(*) FROM realized_vol_history WHERE asset=?",(asset,)).fetchone()[0]
    if existing:
        log("BOOT", f"{asset} realized-vol bootstrap already has {existing} days")
        return existing
    log("BOOT", f"Fetching {asset} realized-vol history from Deribit")
    raw=call("get_historical_volatility",currency=asset)
    records=[]
    for point in raw:
        try:
            timestamp, value=point[0], float(point[1])
            day=datetime.fromtimestamp(timestamp/1000,timezone.utc).strftime("%Y-%m-%d")
            records.append((asset,day,value))
        except (IndexError,TypeError,ValueError): continue
    with sqlite3.connect(DB) as c:
        c.executemany("INSERT OR REPLACE INTO realized_vol_history VALUES(?,?,?)",records)
    log("BOOT", f"{asset} realized-vol bootstrap saved {len(records)} days")
    return len(records)

def rv_bootstrap_stats(asset):
    with sqlite3.connect(DB) as c:
        rows=c.execute("SELECT rv FROM realized_vol_history WHERE asset=? ORDER BY day DESC LIMIT 31",(asset,)).fetchall()
        total=c.execute("SELECT COUNT(*) FROM realized_vol_history WHERE asset=?",(asset,)).fetchone()[0]
        prices=dict(c.execute("SELECT day,spot FROM spot_history WHERE asset=?",(asset,)).fetchall())
        rv_rows=c.execute("SELECT day,rv FROM realized_vol_history WHERE asset=? ORDER BY day DESC LIMIT 31",(asset,)).fetchall()
    values=[float(x[0]) for x in reversed(rows)]
    changes=[values[i]-values[i-1] for i in range(1,len(values))]
    vvol=math.sqrt(sum((x-sum(changes)/len(changes))**2 for x in changes)/(len(changes)-1)) if len(changes)>1 else None
    aligned=[(day,float(rv),float(prices[day])) for day,rv in reversed(rv_rows) if day in prices]
    returns=[math.log(aligned[i][2]/aligned[i-1][2]) for i in range(1,len(aligned)) if aligned[i-1][2]>0]
    rv_changes=[aligned[i][1]-aligned[i-1][1] for i in range(1,len(aligned))]
    rho=correlation(returns,rv_changes)
    return {"realizedDays":total,"realizedMinimum":30,"realizedVolOfVol":round(vvol,4) if vvol is not None else None,"rhoRealized":round(rho,4) if rho is not None else None,"realizedStatus":"solid" if total>=30 else "accumulating"}

def correlation(xs,ys):
    if len(xs)<3 or len(xs)!=len(ys): return None
    mx=sum(xs)/len(xs); my=sum(ys)/len(ys)
    numerator=sum((x-mx)*(y-my) for x,y in zip(xs,ys)); dx=sum((x-mx)**2 for x in xs); dy=sum((y-my)**2 for y in ys)
    return numerator/math.sqrt(dx*dy) if dx>0 and dy>0 else None

def bootstrap_spot_history(asset):
    with sqlite3.connect(DB) as c:
        existing=c.execute("SELECT COUNT(*) FROM spot_history WHERE asset=?",(asset,)).fetchone()[0]
    if existing: return existing
    end=int(time.time()*1000); start=end-365*86400000
    log("BOOT", f"Fetching {asset} daily perpetual closes for realized correlation")
    chart=call("get_tradingview_chart_data",instrument_name=f"{asset}-PERPETUAL",start_timestamp=start,end_timestamp=end,resolution="1D")
    ticks, closes=chart.get("ticks",[]),chart.get("close",[])
    records=[(asset,datetime.fromtimestamp(t/1000,timezone.utc).strftime("%Y-%m-%d"),float(c)) for t,c in zip(ticks,closes) if c]
    with sqlite3.connect(DB) as c: c.executemany("INSERT OR REPLACE INTO spot_history VALUES(?,?,?)",records)
    log("BOOT", f"{asset} spot-history bootstrap saved {len(records)} days")
    return len(records)

def ravagli_record(asset, spot, selected, rvstats):
    if not selected: return None
    # Transparent proxy calibration: c maps 25Δ smile curvature to v_imp;
    # d maps normalized RR to implied spot-vol correlation. Both are UI-labelled.
    c, d = 4.0, 2.5
    tenor=max(selected["days"],1); root=math.sqrt(tenor/365)
    v_imp=c*abs(selected["fly"])/max(math.sqrt(365/tenor),1)
    rho_imp=max(-1,min(1,d*selected["rr"]/(max(selected["atmIv"],.01)*root)))
    v_r=rvstats.get("realizedVolOfVol"); rho_r=rvstats.get("rhoRealized")
    skew_rp=(rho_r*v_r-rho_imp*v_imp)*(selected["atmIv"]/100) if v_r is not None and rho_r is not None else None
    vvol_rp=v_imp*v_imp-v_r*v_r if v_r is not None else None
    return {"date":datetime.now(timezone.utc).strftime("%Y-%m-%d"),"spot":spot,"tenor":round(tenor,1),"expiry":selected["expiry"],"atmIv":selected["atmIv"],"rr":selected["rr"],"fly":selected["fly"],"c25Delta":selected.get("c25Delta"),"p25Delta":selected.get("p25Delta"),"vImp":round(v_imp,4),"rhoImp":round(rho_imp,4),"vR":v_r,"rhoR":rho_r,"skewRP":round(skew_rp,4) if skew_rp is not None else None,"vvolRP":round(vvol_rp,4) if vvol_rp is not None else None,"hedgeRatio":selected.get("hedgeRatio")}

def save_snapshot(asset, data):
    day=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slim={"at":data["updatedAt"],"spot":data["spot"],"surface":data["surface"],"rv7":data["rv7"],"netGamma":data["summary"]["netGamma"],"ravagli":data.get("ravagli")}
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?)",(asset,day,json.dumps(slim)))
        c.execute("INSERT OR REPLACE INTO squeeze_snapshots VALUES(?,?,?)",(asset,data["updatedAt"],json.dumps(data["levels"])))
        c.execute("INSERT OR REPLACE INTO oi_snapshots VALUES(?,?,?)",(asset,data["updatedAt"],json.dumps(data.get("oiByStrike",{}))))
        c.execute("INSERT OR REPLACE INTO market_snapshots VALUES(?,?,?)",(asset,data["updatedAt"],json.dumps({"perp":data.get("regime",{}).get("inputs",{}).get("perp",{}),"optionOiSplit":data.get("optionOiSplit",{}),"optionActivity":data.get("optionActivity",{}),"flow":data.get("flow",{}),"signal":data.get("regime",{}).get("inputs",{}).get("signal",{})})))
        c.execute("DELETE FROM squeeze_snapshots WHERE asset=? AND rowid NOT IN (SELECT rowid FROM squeeze_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT 1000)",(asset,asset))
        c.execute("DELETE FROM oi_snapshots WHERE asset=? AND rowid NOT IN (SELECT rowid FROM oi_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT 1000)",(asset,asset))
        c.execute("DELETE FROM market_snapshots WHERE asset=? AND rowid NOT IN (SELECT rowid FROM market_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT 50000)",(asset,asset))

def load_history(asset):
    with sqlite3.connect(DB) as c: rows=c.execute("SELECT payload FROM snapshots WHERE asset=? ORDER BY day DESC LIMIT 365",(asset,)).fetchall()
    return [json.loads(x[0]) for x in rows]

def previous_oi(asset, current_at):
    with sqlite3.connect(DB) as c:
        row=c.execute("SELECT captured_at,payload FROM oi_snapshots WHERE asset=? AND captured_at<? ORDER BY captured_at DESC LIMIT 1",(asset,current_at)).fetchone()
    return (row[0],json.loads(row[1])) if row else (None,{})

def previous_market(asset, current_at):
    with sqlite3.connect(DB) as c:
        row=c.execute("SELECT captured_at,payload FROM market_snapshots WHERE asset=? AND captured_at<? ORDER BY captured_at DESC LIMIT 1",(asset,current_at)).fetchone()
    return (row[0],json.loads(row[1])) if row else (None,{})

def market_history(asset, limit=10080):
    """Chronological local context.  10,080 one-minute records is ~7 days."""
    with sqlite3.connect(DB) as c:
        rows=c.execute("SELECT payload FROM market_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT ?",(asset,limit)).fetchall()
    return [json.loads(row[0]) for row in reversed(rows)]

def mean(values): return sum(values)/len(values) if values else None

def percentile_of(value, values):
    if value is None or len(values)<30: return None
    return sum(1 for item in values if item<=value)/len(values)*100

def signal_values(history, group, key):
    values=[]
    for item in history:
        value=(item.get(group) or {}).get(key)
        if isinstance(value,(int,float)) and math.isfinite(value): values.append(float(value))
    return values

def apply_hysteresis(asset, candidate, taken_at, confirmations=3):
    """Require repeated candidate labels before changing the displayed state."""
    with sqlite3.connect(DB) as c:
        row=c.execute("SELECT stable_label,pending_label,pending_count FROM regime_state WHERE asset=?",(asset,)).fetchone()
        stable,pending,count=row if row else (candidate,None,0)
        changed=False
        if candidate==stable:
            pending,count=None,0
        elif candidate==pending:
            count+=1
        else:
            pending,count=candidate,1
        if pending and count>=confirmations:
            stable,pending,count=pending,None,0; changed=True
        c.execute("INSERT OR REPLACE INTO regime_state VALUES(?,?,?,?,?)",(asset,stable,pending,count,taken_at))
    return {"label":stable,"candidate":candidate,"pending":pending,"count":count,"required":confirmations,"changed":changed}

def settle_forward_outcomes(asset, now_at, current_spot):
    """Fill outcome fields after 1h/4h/24h using the first later live price."""
    now_dt=datetime.fromisoformat(now_at)
    horizons=(("forward_1h",1), ("forward_4h",4), ("forward_24h",24))
    with sqlite3.connect(DB) as c:
        rows=c.execute("SELECT captured_at,payload,forward_1h,forward_4h,forward_24h FROM regime_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT 5000",(asset,)).fetchall()
        for captured,payload,*outcomes in rows:
            try:
                record=json.loads(payload); start=float(record["spot"]); age=(now_dt-datetime.fromisoformat(captured)).total_seconds()/3600
            except (ValueError,KeyError,TypeError):
                continue
            if start<=0: continue
            for (column,hours),outcome in zip(horizons,outcomes):
                if outcome is None and age>=hours:
                    c.execute(f"UPDATE regime_snapshots SET {column}=? WHERE asset=? AND captured_at=?",(current_spot/start-1,asset,captured))

def save_regime_snapshot(asset, taken_at, regime, spot):
    payload={"spot":spot,"label":regime["label"],"candidateLabel":regime["candidateLabel"],"directionScore":regime["directionScore"],"accelerationScore":regime["accelerationScore"],"expectedMove1d":regime["expectedMove1d"],"blocks":regime["blocks"]}
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO regime_snapshots(asset,captured_at,payload) VALUES(?,?,?)",(asset,taken_at,json.dumps(payload)))
        c.execute("DELETE FROM regime_snapshots WHERE asset=? AND rowid NOT IN (SELECT rowid FROM regime_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT 50000)",(asset,asset))

def evaluation_summary(asset):
    with sqlite3.connect(DB) as c:
        rows=c.execute("SELECT payload,forward_1h,forward_4h,forward_24h FROM regime_snapshots WHERE asset=? ORDER BY captured_at DESC LIMIT 50000",(asset,)).fetchall()
    result={"asset":asset,"records":len(rows),"horizons":{}}
    for column,hours in ((1,1),(2,4),(3,24)):
        completed=[]
        for row in rows:
            outcome=row[column]
            if outcome is None: continue
            try: completed.append((json.loads(row[0]),float(outcome)))
            except (ValueError,TypeError,json.JSONDecodeError): pass
        directional=[(record,outcome) for record,outcome in completed if abs(record.get("directionScore",0))>=20 and abs(outcome)>=.0001]
        hits=sum(1 for record,outcome in directional if (record["directionScore"]>0)==(outcome>0))
        high=[abs(outcome) for record,outcome in completed if record.get("accelerationScore",0)>=63]
        all_moves=[abs(outcome) for _,outcome in completed]
        result["horizons"][f"{hours}h"]={"completed":len(completed),"directionalSignals":len(directional),"directionHitRate":round(hits/len(directional)*100,1) if directional else None,"highAccelerationAvgMove":round(mean(high)*100,3) if high else None,"baselineAvgMove":round(mean(all_moves)*100,3) if all_moves else None,"status":"ready" if len(completed)>=30 else "accumulating"}
    return result

def clamp(value, low, high): return max(low,min(high,value))

def perp_context(asset):
    """Public Deribit perpetual inputs plus a bounded Deribit trade-tape proxy."""
    ticker=call("ticker",instrument_name=f"{asset}-PERPETUAL")
    end=int(time.time()*1000)
    chart=call("get_tradingview_chart_data",instrument_name=f"{asset}-PERPETUAL",start_timestamp=end-30*3600000,end_timestamp=end,resolution="60")
    closes=[float(x) for x in chart.get("close",[]) if x]
    costs=[float(x) for x in chart.get("cost",[]) if x is not None]
    def change(back):
        return (closes[-1]/closes[-1-back]-1) if len(closes)>back and closes[-1-back]>0 else None
    recent=costs[-4:]
    baseline=costs[-24:-4]
    volume_ratio=sum(recent)/len(recent)/(sum(baseline)/len(baseline)) if recent and baseline and sum(baseline)>0 else None
    stats=ticker.get("stats") or {}
    return {
        "perpPrice":float(ticker.get("mark_price") or ticker.get("last_price") or 0),
        "return4h":change(4), "return24h":change(24), "volumeRatio4h":volume_ratio,
        "perpOpenInterest":float(ticker.get("open_interest") or 0),
        "funding8h":float(ticker.get("funding_8h") or 0),
        "currentFunding":float(ticker.get("current_funding") or 0),
        "volume24hUsd":float(stats.get("volume_usd") or stats.get("volume_notional") or 0),
        "priceChange24h":float(stats.get("price_change") or 0)/100,
        "takerFlow":taker_flow(asset),
    }

def taker_flow(asset):
    """Recent Deribit perpetual trade tape; deliberately not labelled global CVD."""
    try:
        raw=call("get_last_trades_by_instrument",instrument_name=f"{asset}-PERPETUAL",count=1000,sorting="desc")
        trades=raw.get("trades",[]) if isinstance(raw,dict) else []
        buys=sells=0.0; timestamps=[]; liquidations=0; liquidation_notional=0.0
        for trade in trades:
            try:
                amount=float(trade.get("amount") or 0)
                side=trade.get("direction")
                if side=="buy": buys+=amount
                elif side=="sell": sells+=amount
                timestamp=trade.get("timestamp")
                if timestamp: timestamps.append(int(timestamp))
                if trade.get("liquidation"):
                    liquidations+=1; liquidation_notional+=amount
            except (TypeError,ValueError):
                continue
        total=buys+sells; window=(max(timestamps)-min(timestamps))/1000 if len(timestamps)>1 else 0
        return {"available":bool(total),"sampleTrades":len(trades),"windowSeconds":round(window,1),"buyNotional":round(buys,2),"sellNotional":round(sells,2),"takerImbalance":round((buys-sells)/total,4) if total else None,"signedNotional":round(buys-sells,2),"liquidationCount":liquidations,"liquidationNotional":round(liquidation_notional,2),"liquidationNote":"Deribit public tape only; any liquidation flag can be delayed and is not cross-exchange coverage."}
    except Exception as e:
        log("FLOW", f"{asset} trade tape unavailable: {type(e).__name__}: {e}")
        return {"available":False,"status":"Deribit trade tape unavailable for this refresh."}

def summarize_option_activity(book_summary, previous=None):
    """Rolling 24h option activity and OI context, never inferred as buyer/seller flow."""
    totals={"call":0.0,"put":0.0}; volume={"call":0.0,"put":0.0}
    for item in book_summary:
        try:
            side="call" if item["instrument_name"].endswith("-C") else "put" if item["instrument_name"].endswith("-P") else None
            if not side: continue
            totals[side]+=float(item.get("open_interest") or 0)
            volume[side]+=float(item.get("volume_usd") or 0)
        except (KeyError,TypeError,ValueError):
            continue
    total_volume=volume["call"]+volume["put"]
    previous=previous or {}
    old_total=float(previous.get("volumeUsd") or 0)
    delta=total_volume-old_total if old_total else None
    return {"volumeUsd":round(total_volume,2),"callVolumeUsd":round(volume["call"],2),"putVolumeUsd":round(volume["put"],2),"putCallVolumeRatio":round(volume["put"]/volume["call"],3) if volume["call"] else None,"volumeDeltaSinceSnapshot":round(delta,2) if delta is not None else None,"callOi":round(totals["call"],2),"putOi":round(totals["put"],2),"note":"Rolling 24h Deribit option volume and OI; it does not identify buyer/seller initiation."}

def market_regime(asset, spot, metrics, selected, positioning, option_oi, option_oi_split, perp, option_activity, history, taken_at):
    """Capped, explainable regime model.  It diagnoses conditions, not trades."""
    em1=max((selected["atmIv"]/100)*math.sqrt(1/365),.002) if selected else .01
    r4=perp.get("return4h")
    rr=selected.get("rr") if selected else None
    skew=selected.get("skew") if selected else None
    previous=history[-1] if history else {}
    previous_perp=previous.get("perp") or {}
    previous_split=previous.get("optionOiSplit") or {}
    rr_values=signal_values(history,"signal","rr")
    funding_values=signal_values(history,"perp","funding8h")
    rr_baseline=mean(rr_values)
    rr_delta=rr-rr_baseline if rr is not None and rr_baseline is not None and len(rr_values)>=30 else None
    funding=perp.get("funding8h")
    funding_percentile=percentile_of(funding,funding_values)
    blocks={}; drivers=[]; cautions=[]
    missing=["Cross-exchange long/short liquidations", "Verified dealer option positions"]

    # 1) Momentum: one source, normalized to current 1D implied move, cap ±30.
    momentum=clamp((r4 or 0)/em1*18,-30,30) if r4 is not None else 0
    blocks["momentum"]={"name":"Momentum harga","score":round(momentum,1),"cap":30,"detail":f"4h {r4*100:+.2f}% / expected move 1D {em1*100:.2f}%" if r4 is not None else "Riwayat harga belum cukup."}
    if r4 is not None: drivers.append(f"Momentum perpetual 4h {r4*100:+.2f}% ({momentum:+.1f}/30, dinormalisasi IV).")
    else: missing.append("Momentum perpetual 4h")

    # 2) Leverage: treat price and OI as a single interaction, never as two votes.
    oi_change=None; leverage=0; leverage_accel=0; leverage_detail="Menunggu baseline OI perpetual."
    if previous_perp.get("perpOpenInterest"):
        oi_change=perp["perpOpenInterest"]/previous_perp["perpOpenInterest"]-1
        move_sign=1 if momentum>=3 else -1 if momentum<=-3 else 0
        oi_sign=1 if oi_change>=.0005 else -1 if oi_change<=-.0005 else 0
        if move_sign>0 and oi_sign>0:
            leverage,leverage_accel,leverage_detail=14,10,"Harga naik + OI naik: leverage masuk searah."
        elif move_sign>0 and oi_sign<0:
            leverage,leverage_accel,leverage_detail=5,2,"Harga naik + OI turun: short covering / tren lebih rapuh."
        elif move_sign<0 and oi_sign>0:
            leverage,leverage_accel,leverage_detail=-14,10,"Harga turun + OI naik: leverage short bertambah."
        elif move_sign<0 and oi_sign<0:
            leverage,leverage_accel,leverage_detail=-5,7,"Harga turun + OI turun: deleveraging / long liquidation proxy."
        else:
            leverage_detail="Harga atau OI belum bergerak cukup untuk klasifikasi."
    else: missing.append("Interaksi harga × perpetual OI (menunggu snapshot berikutnya)")
    blocks["leverage"]={"name":"Harga × OI perpetual","score":leverage,"cap":20,"detail":leverage_detail,"oiChange":oi_change}
    (drivers if leverage_accel>0 else cautions).append(leverage_detail)

    # 3) Option sentiment: changes vs local baseline; combines RR and option OI under one ±15 cap.
    rr_score=clamp((rr_delta or 0)/1.25*11,-11,11) if rr_delta is not None else 0
    option_oi_score=0; option_detail=[]
    if previous_split:
        call_change=option_oi_split["call"]-float(previous_split.get("call") or option_oi_split["call"])
        put_change=option_oi_split["put"]-float(previous_split.get("put") or option_oi_split["put"])
        option_oi_score=clamp((call_change-put_change)/max(option_oi,1)*900,-5,5)
        option_detail.append(f"ΔOI relatif {'call' if option_oi_score>=0 else 'put'} lebih besar")
    else: missing.append("Perubahan OI opsi (menunggu snapshot berikutnya)")
    sentiment=clamp(rr_score+option_oi_score,-15,15)
    if rr_delta is None: option_detail.insert(0,"ΔRR menunggu ≥30 snapshot lokal")
    else: option_detail.insert(0,f"ΔRR 25Δ {rr_delta:+.2f} poin vs baseline lokal")
    blocks["options"]={"name":"Sentimen opsi","score":round(sentiment,1),"cap":15,"detail":"; ".join(option_detail),"rrDelta":rr_delta,"rrBaseline":rr_baseline}
    (drivers if sentiment>=0 else cautions).append(blocks["options"]["detail"]+".")

    # 4) Gamma structure: only a limited direction modifier; acceleration is separate.
    flip=positioning.get("gammaFlip")
    flip_distance=(spot-flip)/(spot*em1) if flip and spot and em1 else None
    gamma_direction=clamp((flip_distance or 0)*5,-10,10)
    gamma_accel=16 if metrics["netGamma"]<0 else -9
    gamma_detail=(f"Zero-GEX {flip_distance:+.2f} expected-move dari spot." if flip_distance is not None else "Zero-GEX tidak ditemukan dalam grid.")
    gamma_detail+= " Net GEX model negatif memperbesar potensi gerak." if metrics["netGamma"]<0 else " Net GEX model positif dapat meredam gerak."
    blocks["gamma"]={"name":"Struktur gamma","score":round(gamma_direction,1),"cap":10,"acceleration":gamma_accel,"detail":gamma_detail,"flipDistanceExpectedMove":flip_distance}
    (drivers if metrics["netGamma"]<0 else cautions).append(gamma_detail)

    # 5) Volume confirms intensity, never creates direction alone.
    volume_ratio=perp.get("volumeRatio4h"); volume_accel=0
    if volume_ratio is None:
        volume_detail="Rasio volume belum cukup."; missing.append("Konfirmasi volume perpetual")
    elif volume_ratio>=1.2 and abs(momentum)>=3:
        volume_accel=clamp((volume_ratio-1)*12,0,12); volume_detail=f"Notional 4h {volume_ratio:.2f}× baseline 20h: mengonfirmasi gerak."
    elif volume_ratio<.8:
        volume_accel=-7; volume_detail=f"Notional 4h {volume_ratio:.2f}× baseline: konfirmasi lemah."
    else: volume_detail=f"Notional 4h {volume_ratio:.2f}× baseline: netral."
    blocks["volume"]={"name":"Konfirmasi volume","score":0,"cap":0,"acceleration":round(volume_accel,1),"detail":volume_detail}
    (drivers if volume_accel>0 else cautions).append(volume_detail)

    # 6) Taker tape: a small, bounded directional vote from Deribit only.
    # It is intentionally not called cross-exchange CVD and cannot dominate price/OI.
    tape=perp.get("takerFlow") or {}; imbalance=tape.get("takerImbalance")
    taker_score=0; taker_detail="Taker tape Deribit belum tersedia."
    if tape.get("available") and imbalance is not None:
        taker_score=clamp(float(imbalance)*10,-10,10)
        side="beli agresif" if imbalance>0 else "jual agresif" if imbalance<0 else "netral"
        taker_detail=f"{side}: imbalance {imbalance*100:+.1f}% dari {tape.get('sampleTrades',0):,} trade / {tape.get('windowSeconds',0):.0f} detik." 
    else:
        missing.append("Taker buy/sell imbalance Deribit")
    blocks["taker"]={"name":"Taker imbalance Deribit","score":round(taker_score,1),"cap":10,"detail":taker_detail,"sampleTrades":tape.get("sampleTrades"),"windowSeconds":tape.get("windowSeconds")}
    (drivers if taker_score>=0 else cautions).append(taker_detail)

    # 7) Option activity is informational only. Rolling 24h volume and OI do
    # not reveal which side initiated the option trade, so it receives no score.
    pcr=option_activity.get("putCallVolumeRatio")
    activity_detail=(f"24h option volume ${option_activity.get('volumeUsd',0):,.0f}; put/call volume {pcr:.2f}×." if pcr is not None else "Aktivitas opsi 24h belum tersedia.")
    blocks["optionActivity"]={"name":"Aktivitas opsi Deribit","score":0,"cap":0,"detail":activity_detail,"putCallVolumeRatio":pcr}

    crowding_penalty=0; funding_detail="Funding percentile menunggu ≥30 snapshot lokal."
    if funding_percentile is not None:
        same_direction=(momentum>3 and funding>0) or (momentum<-3 and funding<0)
        if same_direction and (funding_percentile>=90 or funding_percentile<=10):
            crowding_penalty=-8; funding_detail=f"Funding 8h berada pada persentil {funding_percentile:.0f}: crowding searah tren."
        else: funding_detail=f"Funding 8h persentil {funding_percentile:.0f}: belum ekstrem searah tren."
    else: missing.append("Funding percentile lokal (menunggu ≥30 snapshot)")
    blocks["funding"]={"name":"Funding / crowding","score":0,"cap":0,"acceleration":crowding_penalty,"detail":funding_detail,"percentile":funding_percentile}
    (cautions if crowding_penalty else drivers).append(funding_detail)

    direction=round(clamp(momentum+leverage+sentiment+gamma_direction+taker_score,-75,75),1)
    acceleration=round(clamp(50+gamma_accel+leverage_accel+volume_accel+crowding_penalty,0,100),1)
    if abs(direction)<20:
        candidate="SIDEWAYS / MENUNGGU KONFIRMASI"
    elif direction>0:
        candidate="POTENSI NAIK CEPAT" if acceleration>=65 and direction>=30 else "POTENSI NAIK LAMBAT"
    else:
        candidate="POTENSI TURUN CEPAT" if acceleration>=65 and direction<=-30 else "POTENSI TURUN LAMBAT"
    stable=apply_hysteresis(asset,candidate,taken_at)
    label=stable["label"]
    tone="green" if "NAIK" in label else "red" if "TURUN" in label else "amber"
    directional_blocks=[momentum,leverage,sentiment,gamma_direction,taker_score]
    active_blocks=[score for score in directional_blocks if abs(score)>=3]
    aligned=sum(1 for score in active_blocks if (score>0)==(direction>0)) if abs(direction)>=20 else 0
    confidence="rendah" if len(active_blocks)<2 or abs(direction)<20 else "sedang" if aligned<3 else "menengah-tinggi"
    return {"label":label,"candidateLabel":candidate,"tone":tone,"directionScore":direction,"accelerationScore":acceleration,"confidence":confidence,"expectedMove1d":spot*em1,"drivers":drivers[:6],"cautions":cautions[:6],"missing":missing,"blocks":blocks,"hysteresis":stable,"inputs":{"perp":perp,"perpOiChange":oi_change,"optionCallOi":option_oi_split["call"],"optionPutOi":option_oi_split["put"],"optionOiTotal":option_oi,"optionActivity":option_activity,"signal":{"rr":rr,"skew":skew,"funding8h":funding,"rrDelta":rr_delta,"fundingPercentile":funding_percentile},"source":"Deribit public: perpetual ticker + chart + recent perpetual tape, options book summary"}}

def gamma_flip(rows, spot):
    """Nearest root of the assumed-sign aggregate GEX curve over a ±25% spot grid."""
    points=[]
    for i in range(101):
        simulated=spot*(.75+i*.005); total=0
        for r in rows:
            sign=-1 if r["type"]=="call" else 1
            gamma=greeks(simulated,r["strike"],max(days(r["expiry"])/365,.00001),r["iv"],r["type"])["gamma"]
            total+=sign*gamma*r["oi"]*simulated*simulated*.01
        points.append((simulated,total))
    candidates=[]
    for (s1,g1),(s2,g2) in zip(points,points[1:]):
        if g1==0 or g1*g2<0:
            root=s1 if g1==0 else s1+(s2-s1)*abs(g1)/(abs(g1)+abs(g2))
            candidates.append(root)
    return min(candidates,key=lambda x:abs(x-spot)) if candidates else None

def daily_scheduler():
    """Runs independently from browser traffic while the backend process is alive."""
    while True:
        now=datetime.now(timezone.utc); target=(now+timedelta(days=1)).replace(hour=0,minute=0,second=5,microsecond=0)
        wait=max(1,(target-now).total_seconds())
        log("SCHED", f"Next automatic snapshot at {target.isoformat()} ({wait/3600:.1f}h)")
        time.sleep(wait)
        for asset in ("BTC","ETH"):
            try:
                CACHE.pop(asset,None); fetch(asset)
                log("SCHED", f"{asset} daily snapshot saved successfully")
            except Exception as e: log("SCHED", f"{asset} daily snapshot FAILED: {type(e).__name__}: {e}")

def bootstrap_worker():
    for asset in ("BTC","ETH"):
        try:
            bootstrap_realized_vol(asset)
            bootstrap_spot_history(asset)
        except Exception as e: log("BOOT", f"{asset} realized-vol bootstrap FAILED: {type(e).__name__}: {e}")

def fetch(asset):
    now=time.time(); cached=CACHE.get(asset)
    if cached and now-cached[0]<TTL:
        log("CACHE", f"{asset} snapshot age {now-cached[0]:.0f}s")
        return cached[1]
    log("INFO", f"Building fresh {asset} options snapshot")
    instruments=call("get_instruments",currency=asset,kind="option",expired="false")
    log("OK", f"{asset}: {len(instruments)} active option instruments")
    summary=call("get_book_summary_by_currency",currency=asset,kind="option")
    log("OK", f"{asset}: {len(summary)} live book summaries")
    index=call("get_index_price",index_name=f"{asset.lower()}_usd")["index_price"]
    log("OK", f"{asset}: index ${index:,.2f}")
    perp=perp_context(asset)
    log("OK", f"{asset}: perpetual context (4h {perp.get('return4h')}, funding {perp.get('funding8h')})")
    books={x["instrument_name"]:x for x in summary}
    rows=[]
    for ins in instruments:
        b=books.get(ins["instrument_name"],{}); oi=float(b.get("open_interest") or 0); iv=float(b.get("mark_iv") or 0)/100
        if oi<=0 or iv<=0: continue
        typ=ins["option_type"]; strike=float(ins["strike"]); expiry=ins["expiration_timestamp"]; g=greeks(index,strike,max(days(expiry)/365,.00001),iv,typ)
        size=float(ins.get("contract_size") or 1)
        # Model assumption: customers are net long; dealer is short calls and long puts.
        dealer_sign=-1 if typ=="call" else 1
        rows.append({"name":ins["instrument_name"],"strike":strike,"type":typ,"expiry":expiry,"iv":iv,"oi":oi,"delta":g["delta"],"gamma":g["gamma"],"vega":g["vega"],"gex":dealer_sign*g["gamma"]*oi*size*index*index*.01,"vex":dealer_sign*g["vanna"]*oi*size,"dex":dealer_sign*g["delta"]*oi*size*index,"cex":dealer_sign*g["charm"]*oi*size*index/365})
    strikes={}
    for r in rows:
        x=strikes.setdefault(str(r["strike"]),{"strike":r["strike"],"gex":0,"call":0,"put":0,"vex":0})
        x["gex"]+=r["gex"]; x["vex"]+=r["vex"]; x[r["type"]]+=r["gex"]
    strike_rows=sorted(strikes.values(),key=lambda x:x["strike"])
    expiries=sorted({r["expiry"] for r in rows})
    matrix=[]
    for s in strike_rows:
        cell={"strike":s["strike"]}
        for e in expiries: cell[str(e)]=sum(r["gex"] for r in rows if r["strike"]==s["strike"] and r["expiry"]==e)
        matrix.append(cell)
    call_gamma=sum(r["gex"] for r in rows if r["type"]=="call"); put_gamma=sum(r["gex"] for r in rows if r["type"]=="put"); net=call_gamma+put_gamma
    above=[x for x in strike_rows if x["strike"]>index]; below=[x for x in strike_rows if x["strike"]<index]
    def level(xs):
        if not xs:return None
        x=max(xs,key=lambda a:abs(a["gex"])); return {"strike":x["strike"],"gex":x["gex"],"label":("Resistance" if x["gex"]>0 else "Upside Accelerator") if x["strike"]>index else ("Support" if x["gex"]>0 else "Downside Accelerator")}
    rv=realized_vol(asset)
    surf=surface(rows,index)
    selected=min(surf,key=lambda x:abs(x["days"]-30)) if surf else None
    metrics={"netGamma":net,"callGamma":call_gamma,"putGamma":put_gamma,"callWeight":abs(call_gamma)/(abs(call_gamma)+abs(put_gamma))*100 if call_gamma or put_gamma else None,"bias":"BULLISH" if net>0 and abs(call_gamma)>abs(put_gamma) else "BEARISH" if net<0 else "NEUTRAL"}
    premium=pct((selected["atmIv"]/100-rv) if selected and rv else None)
    implied_days=len(load_history(asset)); progress=rv_bootstrap_stats(asset); progress.update({"impliedDays":implied_days,"impliedMinimum":5,"impliedStatus":"solid" if implied_days>=5 else "accumulating"})
    ravagli=ravagli_record(asset,index,selected,progress)
    oi_by_strike={str(s):sum(r["oi"] for r in rows if r["strike"]==s) for s in {r["strike"] for r in rows}}
    option_oi_split={"call":sum(r["oi"] for r in rows if r["type"]=="call"),"put":sum(r["oi"] for r in rows if r["type"]=="put")}
    option_oi_total=option_oi_split["call"]+option_oi_split["put"]
    taken_at=datetime.now(timezone.utc).isoformat()
    previous_at, previous_oi_by_strike=previous_oi(asset,taken_at)
    previous_market_at, previous_market_payload=previous_market(asset,taken_at)
    local_market_history=market_history(asset)
    oi_changes=sorted(({"strike":float(s),"change":oi-previous_oi_by_strike.get(s,0),"oi":oi} for s,oi in oi_by_strike.items()),key=lambda x:x["change"],reverse=True) if previous_at else []
    buckets=[("0–2d",0,2), ("3–7d",2,7), ("8–30d",7,30), (">30d",30,9999)]
    expiry_concentration=[{"label":name,"oi":sum(r["oi"] for r in rows if low<days(r["expiry"])<=high),"gex":sum(abs(r["gex"]) for r in rows if low<days(r["expiry"])<=high)} for name,low,high in buckets]
    expected_move=index*(selected["atmIv"]/100)*math.sqrt(max(selected["days"],1)/365) if selected else None
    positioning={"gammaFlip":gamma_flip(rows,index),"expectedMove":expected_move,"expectedMoveDays":selected["days"] if selected else None,"dex":sum(r["dex"] for r in rows),"charmExposure":sum(r["cex"] for r in rows),"oiChangeSince":previous_at,"topOiChanges":oi_changes[:8],"expiryConcentration":expiry_concentration}
    levels=[level(above),level(below),{"strike":min(strike_rows,key=lambda x:abs(x["strike"]-index))["strike"],"gex":min(strike_rows,key=lambda x:abs(x["strike"]-index))["gex"],"label":"Nearest pin / pivot"}] if strike_rows else []
    for item in levels:
        if item: item["takenAt"]=taken_at
    option_activity=summarize_option_activity(summary,previous_market_payload.get("optionActivity") or {})
    regime=market_regime(asset,index,metrics,selected,positioning,option_oi_total,option_oi_split,perp,option_activity,local_market_history,taken_at)
    positioning.update({"perp":perp,"perpChangeSince":previous_market_at,"regimeLabel":regime["label"]})
    settle_forward_outcomes(asset,taken_at,index)
    evaluation=evaluation_summary(asset)
    data={"asset":asset,"updatedAt":taken_at,"spot":index,"summary":metrics,"strikes":strike_rows,"matrix":matrix,"expiries":[{"id":str(e),"days":round(days(e),1)} for e in expiries],"levels":levels,"surface":surf,"rv7":pct(rv),"volPremium":premium,"dataProgress":progress,"ravagli":ravagli,"positioning":positioning,"oiByStrike":oi_by_strike,"optionOiSplit":option_oi_split,"optionActivity":option_activity,"regime":regime,"evaluation":evaluation,"flow":{"taker":perp.get("takerFlow",{}),"options":option_activity,"liquidations":{"status":"Cross-exchange liquidation coverage needs a separate provider/API key.","deribitTapeNote":(perp.get("takerFlow") or {}).get("liquidationNote")},"limitations":["Taker imbalance covers only recent Deribit perpetual trades, not global spot CVD.","Option volume/OI does not prove buyer/seller initiation."]},"ai":ai_summary(asset,metrics,selected,rv,premium)}
    save_snapshot(asset,data)
    save_regime_snapshot(asset,taken_at,regime,index)
    # Include the snapshot just saved in the UI counter (first run is 1/5,
    # rather than misleadingly displaying 0/5).
    data["dataProgress"]["impliedDays"]=len(load_history(asset))
    data["dataProgress"]["impliedStatus"]="solid" if data["dataProgress"]["impliedDays"]>=5 else "accumulating"
    CACHE[asset]=(now,data); return data

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # Local development dashboard: always serve current UI files after an edit.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/snapshot"):
            asset=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("asset",["BTC"])[0].upper()
            log("HTTP", f"Browser requested snapshot: {asset}")
            try:
                body=json.dumps(fetch(asset)).encode(); self.send_response(200)
                log("HTTP", f"Snapshot {asset}: 200 OK")
            except Exception as e:
                body=json.dumps({"error":str(e)}).encode(); self.send_response(502)
                log("HTTP", f"Snapshot {asset}: 502 — see FAIL line above")
            self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/api/history"):
            asset=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("asset",["BTC"])[0].upper(); body=json.dumps(load_history(asset)).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/api/evaluation"):
            asset=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("asset",["BTC"])[0].upper(); body=json.dumps(evaluation_summary(asset)).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(body); return
        return super().do_GET()
    def log_message(self,*args): pass

if __name__=="__main__":
    init_db()
    log("INFO", "Starting local dashboard server")
    log("INFO", "Browser interface: http://127.0.0.1:8000")
    log("INFO", "Deribit route: direct Python HTTPS (environment proxies bypassed)")
    threading.Thread(target=bootstrap_worker,name="rv-bootstrap",daemon=True).start()
    threading.Thread(target=daily_scheduler,name="daily-snapshot",daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1",8000),Handler).serve_forever()
