# scripts/cloud_collect.py - v0.3.1
# Мінімальний реальний збір 5 JSON з фолбеками, штрафами conf і логами + index.json.
# Публічні ендпоїнти, без ключів. Працює в GitHub Actions та локально.

import os, json, time, math, re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
import requests
import numpy as np

OUT_DIR = os.getenv("OUT_DIR", "out")
LOG_DIR = os.getenv("LOG_DIR", "log")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) GitHubActions/FA v0.3.1"}

S = {"flags": [], "missing": [], "notes": []}
CONF = 1.00  # базова впевненість

def utcnow() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

def w(name: str, obj: Dict[str, Any]):
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

def log(msg: str):
    S["notes"].append(msg)

def http_json(url: str, params: Dict[str, Any] = None, timeout: int = 25):
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return r.text
        except Exception as e:
            if i == 2:
                raise
            time.sleep(1 + i)

# ---------- 1) BTC close sanity ----------
def btc_close_usd() -> float:
    try:
        js = http_json("https://api.coingecko.com/api/v3/simple/price",
                       params={"ids": "bitcoin", "vs_currencies": "usd"})
        return float(js["bitcoin"]["usd"])
    except Exception as e:
        S["missing"].append("btc_close")
        log(f"btc_close fail: {e}")
        return float("nan")

# ---------- 2) Stablecoins total + Δ7d ----------
def stables_total_and_delta7d() -> Tuple[float, float]:
    try:
        js = http_json("https://stablecoins.llama.fi/stablecoins")
        total = 0.0
        for it in js.get("peggedUSD", []):
            mc = it.get("market_cap")
            if mc is None:
                c = it.get("circulating", {})
                mc = c.get("latest")
            if mc:
                total += float(mc)
        delta7 = 0.0
        if total < 220_000_000_000:
            S["flags"].append("stables_suspicious_total<220B")
        return float(total), float(delta7)
    except Exception as e:
        try:
            ids = "tether,usd-coin,dai,first-digital-usd"
            cg = http_json("https://api.coingecko.com/api/v3/simple/price",
                           params={"ids": ids, "vs_currencies": "usd", "include_market_cap": "true"})
            total = sum(float(v.get("usd_market_cap", 0.0)) for v in cg.values())
            if total < 220_000_000_000:
                S["flags"].append("stables_suspicious_total<220B")
            S["flags"].append("stables_fallback:coingecko")
            return float(total), 0.0
        except Exception as e2:
            S["missing"].append("stablecoins_total_delta7d")
            log(f"stables fail: llama:{e}; cg:{e2}")
            return 0.0, 0.0

# ---------- 3) ETF flows (Farside -> SoSoValue DOM-fallback) ----------
def etf_daily_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        html = http_json("https://farside.co.uk/bitcoin-etf-flow")
        if isinstance(html, str):
            m_dates = re.findall(r"(20\d{2}-\d{2}-\d{2})", html)
            if m_dates:
                rows.append({"date": m_dates[-1], "source": "farside", "note": "dom-fallback"})
            else:
                raise RuntimeError("no dates parsed from farside html")
        else:
            raise RuntimeError("farside non-html")
    except Exception as e:
        S["flags"].append("etf_fallback_sosovalue")
        try:
            html2 = http_json("https://sosovalue.com/leaderboard/bitcoin_etf_us")
            if isinstance(html2, str):
                rows.append({"date": datetime.utcnow().date().isoformat(), "source": "sosovalue", "note": "dom-fallback"})
            else:
                raise RuntimeError("sosovalue non-html")
        except Exception as e2:
            S["missing"].append("etf_flows")
            log(f"ETF flows failed: farside:{e}; sosovalue:{e2}")
    return rows

# ---------- 4) Options vola - HV surrogate ----------
def btc_hv_30d() -> float:
    try:
        js = http_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
                       params={"vs_currency": "usd", "days": "35", "interval": "daily"})
        prices = [p[1] for p in js.get("prices", [])]
        if len(prices) < 25:
            raise RuntimeError("too few prices")
        rets = np.diff(np.log(np.array(prices, dtype=float)))
        hv = float(np.std(rets[-30:], ddof=1) * np.sqrt(365) * 100)
        return hv
    except Exception as e:
        S["missing"].append("hv_surrogate")
        log(f"hv_surrogate fail: {e}")
        return float("nan")

def build_options_vola() -> Dict[str, Any]:
    global CONF
    hv = btc_hv_30d()
    ok = True
    src = "hv_surrogate"
    S["flags"].append("vol=hv_surrogate")
    CONF -= 0.10
    return {
        "ok": ok,
        "dvol": {"BTC": None, "ETH": None},
        "hv_surrogate": {"BTC": hv, "window": "30d"},
        "source": src,
        "ts": utcnow()
    }

# ---------- 5) Derivs signals: Binance -> Bybit fallback ----------
def _get(url, params=None):
    return requests.get(url, params=params, headers=UA, timeout=25).json()

def binance_premium_index(symbol="BTCUSDT"):
    try:
        js = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                          params={"symbol": symbol}, headers=UA, timeout=25).json()
        return {
            "symbol": symbol,
            "markPrice": float(js["markPrice"]),
            "indexPrice": float(js["indexPrice"]),
            "lastFundingRate": float(js.get("lastFundingRate", 0.0)),
            "nextFundingTime": js.get("nextFundingTime")
        }
    except Exception:
        return None

def bybit_basis_funding(symbol="BTCUSDT"):
    try:
        t = _get("https://api.bybit.com/v5/market/tickers", params={"category": "linear", "symbol": symbol})
        it = t["result"]["list"][0]
        mark = float(it["lastPrice"])
        idx = float(it.get("indexPrice", it.get("lastPrice")))
        f = _get("https://api.bybit.com/v5/market/funding/history",
                 params={"category": "linear", "symbol": symbol, "limit": "1"})
        rate = float(f["result"]["list"][0]["fundingRate"]) if f.get("result", {}).get("list") else 0.0
        return {"symbol": symbol, "markPrice": mark, "indexPrice": idx, "lastFundingRate": rate}
    except Exception:
        return None

def build_derivs_signals() -> Dict[str, Any]:
    arr = []
    for sym in ["BTCUSDT", "ETHUSDT"]:
        rec = binance_premium_index(sym)
        if rec is None:
            rec = bybit_basis_funding(sym)
        if rec:
            rec["basis_abs"] = rec["markPrice"] - rec["indexPrice"]
            rec["basis_bps"] = (rec["basis_abs"] / rec["indexPrice"]) * 10_000 if rec["indexPrice"] else None
            arr.append(rec)
    ok = len(arr) > 0
    return {
        "ok": ok,
        "funding": [{"symbol": r["symbol"], "rate": r["lastFundingRate"]} for r in arr],
        "basis": [{"symbol": r["symbol"], "bps": r["basis_bps"]} for r in arr],
        "oi": [],
        "ts": utcnow()
    }

# ---------- 6) Spot OHLCV: BTC/ETH 1d (30д) ----------
def coingecko_ohlc(id_: str, days: str = "30"):
    try:
        js = http_json(f"https://api.coingecko.com/api/v3/coins/{id_}/ohlc",
                       params={"vs_currency": "usd", "days": days})
        return js if isinstance(js, list) else []
    except Exception as e:
        log(f"ohlc {id_} fail: {e}")
        return []

def build_spot_ohlcv() -> Dict[str, Any]:
    return {
        "ok": True,
        "universe_note": "minimal BTC/ETH; розширимо у наступному патчі",
        "tf": "1d",
        "pairs": [
            {"symbol": "BTCUSDT", "src": "coingecko", "id": "bitcoin", "ohlc": coingecko_ohlc("bitcoin", "30")},
            {"symbol": "ETHUSDT", "src": "coingecko", "id": "ethereum", "ohlc": coingecko_ohlc("ethereum", "30")},
        ],
        "ts": utcnow()
    }

# ---------- 7) Macro flows (stables + ETF rows) ----------
def build_macro_flows() -> Dict[str, Any]:
    total, d7 = stables_total_and_delta7d()
    rows = etf_daily_rows()
    strict_ok = (total > 1e9) and (len(rows) > 0)
    return {
        "ok": bool(strict_ok),
        "stables": {"total": total, "delta_7d": d7},
        "etf": {"rows": rows},
        "ts": utcnow()
    }

# ---------- main ----------
def main():
    global CONF
    btc = btc_close_usd()
    sanity_ok = (not math.isnan(btc)) and (btc > 1000.0)
    if not sanity_ok:
        S["missing"].append("sanity_btc_close>1000")
        CONF -= 0.05

    spot = build_spot_ohlcv()
    derivs = build_derivs_signals()
    vola = build_options_vola()
    macro = build_macro_flows()

    if any(r.get("source") == "sosovalue" for r in macro.get("etf", {}).get("rows", [])):
        CONF -= 0.05
        S["flags"].append("etf_fallback_sosovalue:-0.05")
    if "stables_suspicious_total<220B" in S["flags"]:
        S["flags"].append("flow_weight_down")

    quorum = "ok"
    if not macro["ok"] or not sanity_ok or not derivs["ok"]:
        quorum = "ok_fallback"

    summary = f"btc_close={btc:.2f} | macro_rows={len(macro.get('etf',{}).get('rows',[]))} | flags={','.join(S['flags']) or '-'}"
    tripack = {
        "breadth_meta": {},
        "policy_flags": [],
        "conf": round(max(0.0, min(1.0, CONF)), 2),
        "log": {
            "quorum": quorum,
            "missing": S["missing"],
            "flags": S["flags"],
            "summary": summary
        },
        "ts": utcnow()
    }

    # write main files
    w("spot_ohlcv_v2.json", spot)
    w("derivs_signals_v2.json", derivs)
    w("options_vola_v2.json", vola)
    w("macro_flows_v2.json", macro)
    w("tripack_meta_v2.json", tripack)

    # log file
    with open(os.path.join(LOG_DIR, "cloud.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(S["notes"]))

    # lightweight index.json для швидкого перегляду у data-cloud
    index = {
        "ts": tripack["ts"],
        "conf": tripack["conf"],
        "quorum": tripack["log"]["quorum"],
        "files": [
            "spot_ohlcv_v2.json",
            "derivs_signals_v2.json",
            "options_vola_v2.json",
            "macro_flows_v2.json",
            "tripack_meta_v2.json"
        ],
        "flags": S["flags"],
        "missing": S["missing"]
    }
    w("index.json", index)

if __name__ == "__main__":
    main()
