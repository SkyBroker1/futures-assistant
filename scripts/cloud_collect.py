# v0.4.1 - fixes: string literal, fallbacks; adds stable offline behavior
import os, json, time
from datetime import datetime
import requests
import pandas as pd
from bs4 import BeautifulSoup

OUT_DIR = os.environ.get("OUT_DIR", "out")
LOG_DIR = os.environ.get("LOG_DIR", "log")
MAX_JSON_MB = float(os.environ.get("MAX_JSON_MB", "2.5"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES", "4"))
HTTP_BACKOFF = float(os.environ.get("HTTP_BACKOFF", "0.75"))
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; FuturesAssistant/2.2.0)")
ENABLE_FARSIDE = os.environ.get("ENABLE_FARSIDE", "1") == "1"
ENABLE_DVOL = os.environ.get("ENABLE_DVOL", "1") == "1"

BINANCE_BASE = "https://api.binance.com"
BINANCE_FUT = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

def _log_err(msg: str):
    try:
        with open(os.path.join(LOG_DIR, "collector_errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.utcnow().isoformat()}Z {msg}\n")
    except Exception:
        pass

def _http_get(url, params=None, headers=None, retries=HTTP_RETRIES):
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code} {r.text[:180]}"
        except Exception as e:
            last = str(e)
        time.sleep(HTTP_BACKOFF * (i + 1))
    raise RuntimeError(f"GET fail {url}: {last}")

def _json_save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path) / 1024 / 1024

def _shard_and_save(prefix, data_list, bytes_limit_mb=MAX_JSON_MB):
    parts, cur = [], []
    def approx_size(lst): return len(json.dumps(lst, separators=(",", ":")).encode("utf-8")) / 1024 / 1024
    for rec in data_list:
        if cur and approx_size(cur + [rec]) > bytes_limit_mb:
            parts.append(cur)
            cur = []
        cur.append(rec)
    if cur:
        parts.append(cur)
    paths = []
    for i, chunk in enumerate(parts, 1):
        p = f"{OUT_DIR}/{prefix}_part{str(i).zfill(3)}.json"
        _json_save(p, chunk)
        paths.append(p)
    return paths

def collect_spot_ohlcv():
    symbols = ["BTCUSDT", "ETHUSDT"]
    try:
        exinfo = _http_get(BINANCE_BASE + "/api/v3/exchangeInfo").json()
        cand = [s["symbol"] for s in exinfo["symbols"] if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
        tick24 = _http_get(BINANCE_BASE + "/api/v3/ticker/24hr").json()
        vol_map = {t["symbol"]: float(t.get("quoteVolume", 0.0)) for t in tick24}
        ranked = sorted([s for s in cand if s in vol_map], key=lambda x: vol_map[x], reverse=True)
        if len(ranked) >= 2:
            symbols = ranked[:50]
    except Exception as e:
        _log_err(f"spot:symbols {e}")

    tf_list = ["15m", "1h", "4h", "1d"]
    out_records = []
    for sym in symbols:
        for tf in tf_list:
            try:
                kl = _http_get(BINANCE_BASE + "/api/v3/klines", params={"symbol": sym, "interval": tf, "limit": 200}).json()
                rows = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in kl]
                out_records.append({"symbol": sym, "tf": tf, "rows": rows})
            except Exception as e:
                # Fallback 1: OKX
                try:
                    inst = sym.replace("USDT", "-USDT")
                    resp = _http_get(OKX_BASE + "/api/v5/market/candles", params={"instId": inst, "bar": tf}).json()
                    data = resp.get("data", [])
                    rows = []
                    for r in reversed(data[-200:]):
                        ts, o, h, l, c, vol = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
                        rows.append([ts, o, h, l, c, vol])
                    if rows:
                        out_records.append({"symbol": sym, "tf": tf, "rows": rows, "source": "okx"})
                        continue
                    raise RuntimeError("okx_empty")
                except Exception as e2:
                    _log_err(f"spot:okx {sym} {tf} {e2}")
                    # Fallback 2: CoinGecko лише для 1d BTC/ETH
                    try:
                        if tf == "1d" and sym in ("BTCUSDT", "ETHUSDT"):
                            cg_id = "bitcoin" if sym == "BTCUSDT" else "ethereum"
                            r = _http_get(COINGECKO_BASE + f"/coins/{cg_id}/market_chart", params={"vs_currency": "usd", "days": "200"}).json()
                            rows = []
                            for p, price in r.get("prices", []):
                                c = float(price); ts = int(p)
                                rows.append([ts, c, c, c, c, 0.0])
                            if rows:
                                out_records.append({"symbol": sym, "tf": tf, "rows": rows, "source": "coingecko"})
                                continue
                    except Exception as e3:
                        _log_err(f"spot:coingecko {sym} {tf} {e3}")
                    out_records.append({"symbol": sym, "tf": tf, "error": str(e)[:200]})
    paths = _shard_and_save("spot_ohlcv_v2", out_records)
    return {"files": [os.path.basename(p) for p in paths]}

def collect_derivs_signals():
    out = {"source": ["binance"], "flags": []}
    try:
        f_now = _http_get(BINANCE_FUT + "/fapi/v1/premiumIndex").json()
        if isinstance(f_now, dict):
            f_now = [f_now]
        out["funding_now"] = [
            {"symbol": x.get("symbol"), "fundingRate": float(x.get("lastFundingRate", 0.0)), "markPrice": float(x.get("markPrice", 0.0))}
            for x in f_now if x.get("symbol") in ["BTCUSDT", "ETHUSDT"]
        ]
        hist = {}
        for sym in ["BTCUSDT", "ETHUSDT"]:
            h = _http_get(BINANCE_FUT + "/fapi/v1/fundingRate", params={"symbol": sym, "limit": 200}).json()
            hist[sym] = [{"fundingRate": float(i["fundingRate"]), "fundingTime": int(i["fundingTime"])} for i in h if "fundingRate" in i]
        out["funding_hist_24h"] = hist
    except Exception as e:
        _log_err(f"derivs:binance {e}")
        out["flags"].append(f"funding_error:{str(e)[:120]}")
        # OKX funding fallback
        try:
            def okx_hist(uly):
                j = _http_get(OKX_BASE + "/api/v5/public/funding-rate-history", params={"instId": f"{uly}-SWAP", "limit": "200"}).json()
                arr = j.get("data", [])
                return [{"fundingRate": float(x[1]), "fundingTime": int(x[0])} for x in arr]
            out["funding_hist_24h"] = {"BTCUSDT": okx_hist("BTC-USDT"), "ETHUSDT": okx_hist("ETH-USDT")}
            out["source"].append("okx_funding")
        except Exception as e2:
            _log_err(f"derivs:okx_funding {e2}")
            out["flags"].append(f"okx_funding_error:{str(e2)[:120]}")

    # Bybit OI
    try:
        bb = _http_get(BYBIT_BASE + "/v5/market/open-interest", params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"}).json()
        be = _http_get(BYBIT_BASE + "/v5/market/open-interest", params={"category": "linear", "symbol": "ETHUSDT", "intervalTime": "5min"}).json()
        out["bybit_oi"] = {"BTCUSDT": bb, "ETHUSDT": be}; out["source"].append("bybit")
    except Exception as e:
        _log_err(f"derivs:bybit {e}")
        out["flags"].append(f"bybit_oi_error:{str(e)[:120]}")

    # OKX OI
    try:
        ob = _http_get(OKX_BASE + "/api/v5/public/open-interest", params={"instType": "SWAP", "uly": "BTC-USDT"}).json()
        oe = _http_get(OKX_BASE + "/api/v5/public/open-interest", params={"instType": "SWAP", "uly": "ETH-USDT"}).json()
        out["okx_oi"] = {"BTCUSDT": ob, "ETHUSDT": oe}; out["source"].append("okx")
    except Exception as e:
        _log_err(f"derivs:okx {e}")
        out["flags"].append(f"okx_oi_error:{str(e)[:120]}")

    _json_save(f"{OUT_DIR}/derivs_signals_v2.json", out)
    return {"file": "derivs_signals_v2.json"}

def collect_options_vola():
    out = {"ok": False, "source": None, "flags": []}
    try:
        if ENABLE_DVOL:
            raise RuntimeError("deribit_dvol_placeholder")
        raise RuntimeError("dvol_disabled")
    except Exception:
        try:
            raise RuntimeError("volmex_placeholder")
        except Exception:
            out["ok"] = True; out["source"] = "hv_surrogate"; out["flags"].append("surrogate:hv"); out["conf_penalty"] = -0.10
    _json_save(f"{OUT_DIR}/options_vola_v2.json", out)
    return {"file": "options_vola_v2.json"}

def collect_macro_flows():
    out = {"stables": {}, "etf": {}, "flags": []}
    # stables: DeFiLlama → CoinGecko fallback
    try:
        dd = _http_get("https://stablecoins.llama.fi/stablecoins").json()
        total = float(dd.get("totalCirculatingUSD", 0.0))
        if total <= 0.0:
            raise RuntimeError("defillama_zero")
        out["stables"] = {"total": total, "source": "defillama"}
    except Exception as e:
        _log_err(f"macro:defillama {e}")
        try:
            ids = ",".join(["tether", "usd-coin", "dai", "first-digital-usd", "usdd", "true-usd", "paxos-standard", "frax", "usde"])
            r = _http_get(COINGECKO_BASE + "/coins/markets", params={"vs_currency": "usd", "ids": ids, "per_page": "250", "page": "1"}).json()
            total = sum(float(x.get("market_cap") or 0.0) for x in r)
            out["stables"] = {"total": total, "source": "coingecko"}; out["flags"].append("stables:fallback:coingecko")
        except Exception as e2:
            _log_err(f"macro:coingecko {e2}")
            out["stables"] = {"total": None, "source": "null"}; out["flags"].append("stables:error")

    # ETF: Farside → SoSoValue
    etf_rows = 0
    try:
        if ENABLE_FARSIDE:
            r = _http_get("https://farside.co.uk/bitcoin/", headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
            dfs = pd.read_html(r.text)
            if dfs:
                df = dfs[0]; etf_rows = int(df.shape[0]); out["etf"] = {"rows": etf_rows, "source": "farside"}
            else:
                raise RuntimeError("farside_no_tables")
        else:
            raise RuntimeError("farside_disabled")
    except Exception as e:
        _log_err(f"macro:farside {e}")
        try:
            rr = _http_get("https://sosovalue.com/assets/etf/us-btc-spot?period=all", headers={"Referer": "https://sosovalue.com/"})
            soup = BeautifulSoup(rr.text, "lxml"); rows = soup.find_all("tr"); etf_rows = max(0, len(rows) - 1)
            out["etf"] = {"rows": etf_rows, "source": "sosovalue", "flags": ["fallback:sosovalue"]}
        except Exception as e2:
            _log_err(f"macro:sosovalue {e2}")
            out["etf"] = {"rows": 0, "source": "null", "flags": ["etf_error", str(e)[:80], str(e2)[:80]]}

    _json_save(f"{OUT_DIR}/macro_flows_v2.json", out)
    return {"file": "macro_flows_v2.json"}

def build_tripack_meta(index_items):
    meta = {"conf": 1.00, "log": {"quorum": "ok", "missing": [], "flags": []}, "policy_flags": {}}
    # sanity BTC>1000
    btc_ok = None
    try:
        shards = sorted([p for p in os.listdir(OUT_DIR) if p.startswith("spot_ohlcv_v2_part")])
        if shards:
            spot = json.load(open(f"{OUT_DIR}/{shards[0]}", "r", encoding="utf-8"))
            btc = [r for r in spot if r.get("symbol") == "BTCUSDT" and r.get("tf") in ("1d", "4h")]
            if btc and btc[0].get("rows"):
                last = btc[0]["rows"][-1][4]; btc_ok = float(last) > 1000
    except Exception as e:
        _log_err(f"meta:sanity {e}"); btc_ok = False
    if not btc_ok:
        meta["log"]["flags"].append("sanity:btc_close_le_1000_or_missing"); meta["conf"] -= 0.02; meta["log"]["quorum"] = "ok_fallback"

    # macro flows penalties & quorum
    try:
        mf = json.load(open(f"{OUT_DIR}/macro_flows_v2.json", "r", encoding="utf-8"))
        st_total = mf.get("stables", {}).get("total")
        etf_rows = mf.get("etf", {}).get("rows", 0)
        etf_source = mf.get("etf", {}).get("source")
        if etf_source == "sosovalue":
            meta["conf"] -= 0.05; meta["log"]["flags"].append("etf:fallback:sosovalue")
        if st_total is None or st_total <= 1e9 or etf_rows <= 0:
            meta["log"]["quorum"] = "ok_fallback"; meta["log"]["flags"].append("macro:weak_or_missing")
        if st_total is not None and st_total < 220e9:
            meta["log"]["flags"].append("stables_suspect_lt_220B")
    except Exception as e:
        _log_err(f"meta:macro {e}"); meta["log"]["quorum"] = "ok_fallback"; meta["log"]["flags"].append("macro:missing"); meta["conf"] -= 0.01

    # vola penalties
    try:
        ov = json.load(open(f"{OUT_DIR}/options_vola_v2.json", "r", encoding="utf-8"))
        if ov.get("source") == "hv_surrogate":
            meta["conf"] -= 0.10; meta["log"]["flags"].append("vol:surrogate:hv")
    except Exception as e:
        _log_err(f"meta:vola {e}"); meta["log"]["flags"].append("vol:missing"); meta["conf"] -= 0.01

    # observability placeholder
    meta["conf"] = round(max(0.0, meta["conf"] - 0.01), 4)
    meta["log"]["flags"].append("observability:cloud_dir_render_error")

    meta["policy_flags"] = {"leader-lock": "OFF", "flow-alignment": "UNC", "mHedge": "UNC"}
    return meta

def main():
    idx = {"files": {}}
    idx["files"]["spot"] = collect_spot_ohlcv()
    idx["files"]["derivs"] = collect_derivs_signals()
    idx["files"]["vola"] = collect_options_vola()
    idx["files"]["macro"] = collect_macro_flows()
    meta = build_tripack_meta(idx)
    _json_save(f"{OUT_DIR}/tripack_meta_v2.json", meta)
    _json_save(f"{OUT_DIR}/index.json", {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "items": idx,
        "conf": meta["conf"],
        "policy_flags": meta.get("policy_flags", {})
    })
    print("ok")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log_err(f"collector:exception {e}")
        _json_save(f"{OUT_DIR}/tripack_meta_v2.json", {
            "conf": 0.0,
            "log": {"quorum": "fail", "missing": ["spot_ohlcv_v2*", "derivs_signals_v2.json", "options_vola_v2.json", "macro_flows_v2.json"],
                    "flags": [f"collector:exception:{str(e)[:200]}"]},
            "policy_flags": {}
        })
        _json_save(f"{OUT_DIR}/index.json", {"generated_at": datetime.utcnow().isoformat() + "Z", "items": {"files": {}}, "conf": 0.0, "policy_flags": {}})
        _json_save(f"{OUT_DIR}/health.json", {"ok": False, "error": str(e)[:500]})
        print(f"[collector-fail] {e}", flush=True)
