# scripts/cloud_collect.py — v0.5.1
# Збір: spot(top-50 USDT, 15m/1h/4h/1d, з шардінгом), derivs(funding now+24h hist, OI Bybit/OKX),
# vola(hv_surrogate зі штрафом), macro(stables DeFiLlama→CG, ETF Farside→SoSoValue),
# meta+index з sanity та policy_flags.

import os, json, time
from datetime import datetime, timedelta, timezone
import requests
import pandas as pd
from bs4 import BeautifulSoup

OUT_DIR = os.environ.get("OUT_DIR", "out")
LOG_DIR = os.environ.get("LOG_DIR", "log")
MAX_JSON_MB = float(os.environ.get("MAX_JSON_MB", "2.5"))
HTTP_TIMEOUT = 20
HTTP_RETRIES = 4
HTTP_BACKOFF = 0.75
USER_AGENT = "Mozilla/5.0 (FuturesAssistant/2.2.0)"

BINANCE_BASE = "https://api.binance.com"
BINANCE_FUT = "https://fapi.binance.com"
BYBIT_BASE  = "https://api.bybit.com"
OKX_BASE    = "https://www.okx.com"
CG_BASE     = "https://api.coingecko.com/api/v3"

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

def _http_get(url, params=None, headers=None, retries=HTTP_RETRIES):
    h = {"User-Agent": USER_AGENT}
    if headers: h.update(headers)
    last=None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r
            last=f"{r.status_code}:{r.text[:160]}"
        except Exception as e:
            last=str(e)
        time.sleep(HTTP_BACKOFF*(i+1))
    raise RuntimeError(f"GET {url} failed: {last}")

def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",",":"))

def _log(msg):
    with open(os.path.join(LOG_DIR, "cloud.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z {msg}\n")

# -------- SPOT ----------
def _spot_symbols_top50_usdt():
    ex = _http_get(BINANCE_BASE+"/api/v3/exchangeInfo").json()
    syms=[s["symbol"] for s in ex["symbols"] if s.get("quoteAsset")=="USDT" and s.get("status")=="TRADING"]
    t24 = _http_get(BINANCE_BASE+"/api/v3/ticker/24hr").json()
    vmap={t["symbol"]: float(t.get("quoteVolume",0.0)) for t in t24}
    ranked=sorted([s for s in syms if s in vmap], key=lambda s: vmap[s], reverse=True)
    if len(ranked)>=2: return ranked[:50]
    return ["BTCUSDT","ETHUSDT"]

def _approx_mb(lst): 
    return len(json.dumps(lst, separators=(",",":")).encode("utf-8"))/1024/1024

def collect_spot():
    symbols=_spot_symbols_top50_usdt()
    tfs=["15m","1h","4h","1d"]
    recs=[]
    for s in symbols:
        for tf in tfs:
            try:
                kl=_http_get(BINANCE_BASE+"/api/v3/klines", params={"symbol":s,"interval":tf,"limit":200}).json()
                rows=[[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in kl]
                recs.append({"symbol":s,"tf":tf,"values":rows,"source":"binance"})
            except Exception as e:
                # OKX fallback
                try:
                    inst = s.replace("USDT","-USDT")
                    r=_http_get(OKX_BASE+"/api/v5/market/candles", params={"instId":inst,"bar":tf}).json()
                    data=r.get("data",[])
                    rows=[]
                    for r0 in reversed(data[-200:]):
                        ts,o,h,l,c,vol=int(r0[0]),float(r0[1]),float(r0[2]),float(r0[3]),float(r0[4]),float(r0[5])
                        rows.append([ts,o,h,l,c,vol])
                    if rows:
                        recs.append({"symbol":s,"tf":tf,"values":rows,"source":"okx"})
                        continue
                    raise RuntimeError("okx_empty")
                except Exception as e2:
                    _log(f"spot:{s}:{tf}:{e2}")
                    if tf=="1d" and s in ("BTCUSDT","ETHUSDT"):
                        try:
                            cg_id="bitcoin" if s=="BTCUSDT" else "ethereum"
                            r=_http_get(CG_BASE+f"/coins/{cg_id}/market_chart", params={"vs_currency":"usd","days":"200"}).json()
                            rows=[]
                            for p, price in r.get("prices",[]):
                                c=float(price); ts=int(p)
                                rows.append([ts,c,c,c,c,0.0])
                            if rows:
                                recs.append({"symbol":s,"tf":tf,"values":rows,"source":"coingecko"})
                        except Exception as e3:
                            _log(f"spot:cg:{s}:{e3}")
                    else:
                        recs.append({"symbol":s,"tf":tf,"values":[],"error":str(e)[:120]})
    # shard
    parts=[]; cur=[]
    for r in recs:
        if cur and _approx_mb(cur+[r])>MAX_JSON_MB:
            parts.append(cur); cur=[]
        cur.append(r)
    if cur: parts.append(cur)
    names=[]
    for i,chunk in enumerate(parts,1):
        name=f"spot_ohlcv_v2_part{str(i).zfill(3)}.json"
        _save(os.path.join(OUT_DIR,name), chunk)
        names.append(name)
    return {"files":names}

# -------- DERIVS ----------
def _cut24h(rows, ts_key):
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    cutoff=now-24*3600*1000
    return [r for r in rows if int(r[ts_key])>=cutoff]

def collect_derivs():
    out={"source":[],"flags":[]}
    # funding now + 24h hist
    try:
        f_now=_http_get(BINANCE_FUT+"/fapi/v1/premiumIndex").json()
        if isinstance(f_now,dict): f_now=[f_now]
        f_now=[x for x in f_now if x.get("symbol") in ("BTCUSDT","ETHUSDT")]
        out["funding_now"]=[{"symbol":x["symbol"],"fundingRate":float(x.get("lastFundingRate",0.0)),"markPrice":float(x.get("markPrice",0.0))} for x in f_now]
        hist={}
        for s in ("BTCUSDT","ETHUSDT"):
            h=_http_get(BINANCE_FUT+"/fapi/v1/fundingRate", params={"symbol":s,"limit":500}).json()
            h=[{"fundingRate":float(i["fundingRate"]), "fundingTime":int(i["fundingTime"])} for i in h if "fundingRate" in i]
            hist[s]=_cut24h(h,"fundingTime")
        out["funding_hist_24h"]=hist
        out["source"].append("binance")
    except Exception as e:
        out["flags"].append(f"funding_error:{e}")

    # OI Bybit/OKX
    try:
        bb=_http_get(BYBIT_BASE+"/v5/market/open-interest", params={"category":"linear","symbol":"BTCUSDT","intervalTime":"5min"}).json()
        be=_http_get(BYBIT_BASE+"/v5/market/open-interest", params={"category":"linear","symbol":"ETHUSDT","intervalTime":"5min"}).json()
        out["bybit_oi_raw"]={"BTCUSDT":bb,"ETHUSDT":be}; out["source"].append("bybit")
    except Exception as e:
        out["flags"].append(f"bybit_oi_error:{e}")

    try:
        ob=_http_get(OKX_BASE+"/api/v5/public/open-interest", params={"instType":"SWAP","uly":"BTC-USDT"}).json()
        oe=_http_get(OKX_BASE+"/api/v5/public/open-interest", params={"instType":"SWAP","uly":"ETH-USDT"}).json()
        out["okx_oi_raw"]={"BTCUSDT":ob,"ETHUSDT":oe}; out["source"].append("okx")
    except Exception as e:
        out["flags"].append(f"okx_oi_error:{e}")

    _save(os.path.join(OUT_DIR,"derivs_signals_v2.json"), out)
    return {"file":"derivs_signals_v2.json"}

# -------- VOL ----------
def collect_vola():
    out={"ok":True,"source":"hv_surrogate","flags":["surrogate:hv"],"conf_penalty":-0.10}
    _save(os.path.join(OUT_DIR,"options_vola_v2.json"), out)
    return {"file":"options_vola_v2.json"}

# -------- MACRO ----------
def _farside_rows(html):
    soup=BeautifulSoup(html,"lxml")
    tables=soup.find_all("table")
    best=None; score_best=0
    for t in tables:
        hdr=[th.get_text(strip=True).lower() for th in t.find_all("th")]
        score=sum(int(k in hdr) for k in ("date","flow","aum","net"))
        if score>score_best:
            score_best=score; best=t
    if not best: return 0
    rows=best.find_all("tr")
    return max(0, len(rows)-1)

def collect_macro():
    out={"stables":{}, "etf":{}, "flags":[]}
    # stables
    try:
        d=_http_get("https://stablecoins.llama.fi/stablecoins").json()
        total=float(d.get("totalCirculatingUSD",0.0))
        if total<=0: raise RuntimeError("zero")
        out["stables"]={"total":total,"source":"defillama"}
    except Exception:
        try:
            ids="tether,usd-coin,dai,first-digital-usd,usdd,true-usd,paxos-standard,frax,usde"
            r=_http_get(CG_BASE+"/coins/markets", params={"vs_currency":"usd","ids":ids,"per_page":"250","page":"1"}).json()
            total=sum(float(x.get("market_cap") or 0.0) for x in r)
            out["stables"]={"total":total,"source":"coingecko"}; out["flags"].append("stables:fallback:coingecko")
        except Exception as e2:
            out["stables"]={"total":None,"source":"null"}; out["flags"].append(f"stables:error:{e2}")
    # etf
    try:
        html=_http_get("https://farside.co.uk/bitcoin/", headers={"Accept":"text/html"}).text
        rows=_farside_rows(html)
        if rows<=0: raise RuntimeError("farside_zero_rows")
        out["etf"]={"rows":rows,"source":"farside"}
    except Exception as e:
        try:
            html=_http_get("https://sosovalue.com/assets/etf/us-btc-spot?period=all", headers={"Referer":"https://sosovalue.com/"}).text
            rows=html.lower().count("<tr>")-1
            out["etf"]={"rows":max(0,rows),"source":"sosovalue","flags":["fallback:sosovalue"]}
        except Exception as e2:
            out["etf"]={"rows":0,"source":"null","flags":["etf_error",str(e)[:80],str(e2)[:80]]}
    _save(os.path.join(OUT_DIR,"macro_flows_v2.json"), out)
    return {"file":"macro_flows_v2.json"}

# -------- META/INDEX ----------
def _btc_close_from_spot():
    # шукаємо BTCUSDT у будь-якому шарді
    shards=sorted([p for p in os.listdir(OUT_DIR) if p.startswith("spot_ohlcv_v2_part")])
    for p in shards:
        try:
            data=json.load(open(os.path.join(OUT_DIR,p),"r",encoding="utf-8"))
            for it in data:
                if (it.get("symbol") or "").upper()=="BTCUSDT" and it.get("values"):
                    return float(it["values"][-1][4])
        except Exception as e:
            _log(f"sanity:read_shard:{p}:{e}")
    # fallback CG
    try:
        r=_http_get(CG_BASE+"/coins/bitcoin/market_chart", params={"vs_currency":"usd","days":"1"}).json()
        return float(r["prices"][-1][1])
    except Exception as e:
        _log(f"sanity:cg:{e}")
        return None

def build_meta_and_index():
    meta={"conf":1.0,"log":{"quorum":"ok","missing":[],"flags":[]}, "policy_flags":{}}
    # sanity BTC
    btc=_btc_close_from_spot()
    if not btc or btc<=1000:
        meta["log"]["flags"].append("sanity:btc_close_le_1000_or_missing")
        meta["conf"]-=0.02
        meta["log"]["quorum"]="ok_fallback"
    # penalties
    try:
        ov=json.load(open(os.path.join(OUT_DIR,"options_vola_v2.json"),"r",encoding="utf-8"))
        if ov.get("source")=="hv_surrogate":
            meta["conf"]-=0.10; meta["log"]["flags"].append("vol:surrogate:hv")
    except Exception:
        meta["conf"]-=0.10; meta["log"]["flags"].append("vol:missing")
    try:
        mf=json.load(open(os.path.join(OUT_DIR,"macro_flows_v2.json"),"r",encoding="utf-8"))
        if mf.get("etf",{}).get("source")=="sosovalue": meta["conf"]-=0.05; meta["log"]["flags"].append("etf:fallback:sosovalue")
        if not (mf.get("stables",{}).get("total",0)>1e9 and mf.get("etf",{}).get("rows",0)>0):
            meta["log"]["quorum"]="ok_fallback"; meta["log"]["flags"].append("macro:weak_or_missing")
        if mf.get("stables",{}).get("total", 1e12)<220e9:
            meta["log"]["flags"].append("stables_suspect_lt_220B")
    except Exception:
        meta["conf"]-=0.01; meta["log"]["quorum"]="ok_fallback"; meta["log"]["flags"].append("macro:missing")
    meta["policy_flags"]={"leader-lock":"OFF","flow-alignment":"UNC","mHedge":"UNC"}

    # index
    idx={"files":{
        "spot":{"files":sorted([n for n in os.listdir(OUT_DIR) if n.startswith("spot_ohlcv_v2_part")])},
        "derivs":{"file":"derivs_signals_v2.json"},
        "vola":{"file":"options_vola_v2.json"},
        "macro":{"file":"macro_flows_v2.json"},
    }}
    _save(os.path.join(OUT_DIR,"tripack_meta_v2.json"), meta)
    _save(os.path.join(OUT_DIR,"index.json"), {
        "generated_at": datetime.utcnow().isoformat()+"Z",
        "items": idx,
        "conf": round(max(0.0, meta["conf"]),2),
        "policy_flags": meta["policy_flags"]
    })

def main():
    # 1) Збір
    collect_spot()
    collect_derivs()
    collect_vola()
    collect_macro()
    # 2) Мета+індекс
    build_meta_and_index()
    print("ok")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        _log(f"collector:exception {e}")
        # аварійний мінімум, щоб воркфлоу міг опублікувати
        _save(os.path.join(OUT_DIR,"tripack_meta_v2.json"),
              {"breadth_meta":{},"policy_flags":{"leader-lock":"OFF","flow-alignment":"UNC","mHedge":"UNC"},
               "conf":0.7,"log":{"quorum":"fail","missing":["macro_flows_v2.json","options_vola_v2.json"],"flags":["collector:exception","vol:missing","macro:missing"]}})
        _save(os.path.join(OUT_DIR,"index.json"),
              {"generated_at": datetime.utcnow().isoformat()+"Z","items":{"files":{"spot":{"files":[]},"derivs":{"file":"derivs_signals_v2.json"},"vola":{"file":"options_vola_v2.json"},"macro":{"file":"macro_flows_v2.json"}}},"conf":0.7,"policy_flags":{"leader-lock":"OFF","flow-alignment":"UNC","mHedge":"UNC"}})
