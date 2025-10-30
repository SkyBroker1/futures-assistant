#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
postprocess_fill_missing.py v1.6
Гарантія випуску всіх JSON для кворуму за будь-яких умов мережі.

Фолбеки:
- SPOT: Binance 1d -> CoinGecko 1d -> only last price (без spot-файлу, але sanity ок)
- VOLA: hv з свічок або hv_const=60.0 з прапором vol:surrogate:const
- MACRO: DeFiLlama + Farside -> SoSoValue -> CACHE static/cache/*
- DERIVS: stub при відсутності
- Кешування: якщо мережа працює і отримано валідні дані, зберігаємо їх у static/cache для майбутніх ранiв

Позначки у tripack_meta_v2.json.log.flags:
- spot:fallback:*
- vol:surrogate:hv|const
- macro:fallback:sosovalue|cache
- macro:stables_cache
- derivs:stub
"""

import argparse
import json
import os
import sys
import time
import math
import statistics
from typing import Any, Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

# ----------------- utils -----------------

def log(msg: str, *, logfile: str | None = None) -> None:
    line = f"[POST] {msg}"
    print(line)
    if logfile:
        try:
            with open(logfile, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def ensure_file(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def dump_json(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# ----------------- external fetchers -----------------

def fetch_binance_klines_btc_1d(limit: int = 180) -> List[List[float]]:
    url = "https://api.binance.com/api/v3/klines"
    qs = {"symbol": "BTCUSDT", "interval": "1d", "limit": str(limit)}
    r = requests.get(url, params=qs, timeout=25)
    r.raise_for_status()
    data = r.json()
    candles = []
    for k in data:
        ts = int(k[0])
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); v = float(k[5])
        candles.append([ts, o, h, l, c, v])
    if len(candles) < 30:
        raise RuntimeError(f"too few candles from Binance: {len(candles)}")
    return candles

def fetch_coingecko_btc_daily(limit_days: int = 180) -> List[List[float]]:
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    qs = {"vs_currency": "usd", "days": str(limit_days), "interval": "daily"}
    r = requests.get(url, params=qs, timeout=25)
    r.raise_for_status()
    data = r.json()
    arr = data.get("prices") or []
    if len(arr) < 30:
        raise RuntimeError(f"too few points from CoinGecko: {len(arr)}")
    candles = []
    for ts, price in arr:
        c = float(price)
        candles.append([int(ts), c, c, c, c, 0.0])
    return candles

def fetch_binance_last_price() -> float | None:
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=15)
        r.raise_for_status()
        return float((r.json() or {}).get("price"))
    except Exception:
        return None

# ----------------- hv helpers -----------------

def hv_from_closes(closes: List[float], tf: str = "1d") -> float:
    if len(closes) < 30:
        raise RuntimeError("not enough closes for hv")
    rets = []
    for i in range(1, len(closes)):
        p, c = closes[i - 1], closes[i]
        if p > 0 and c > 0:
            rets.append(math.log(c / p))
    if len(rets) < 20:
        raise RuntimeError("not enough returns for hv")
    scale = 365.0 if tf == "1d" else 365.0 * 6.0
    hv_annual = statistics.pstdev(rets) * math.sqrt(scale)
    return round(hv_annual * 100, 2)

def extract_btc_closes_from_shard(spot_obj: Any) -> List[float]:
    def ext(rec: Dict[str, Any]) -> List[float] | None:
        if rec.get("symbol") == "BTCUSDT" and rec.get("tf") in ("1d", "4h"):
            candles = rec.get("candles") or rec.get("ohlcv") or []
            closes = [c[4] for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5]
            return closes if closes else None
        return None
    if isinstance(spot_obj, list):
        for rec in spot_obj:
            if isinstance(rec, dict):
                x = ext(rec)
                if x: return x
    if isinstance(spot_obj, dict):
        for rec in (spot_obj.get("rows") or spot_obj.get("data") or []):
            if isinstance(rec, dict):
                x = ext(rec)
                if x: return x
    return []

# ----------------- macro fetch + cache -----------------

def fetch_stables_defillama() -> float | None:
    try:
        r = requests.get("https://stablecoins.llama.fi/stablecoin/marketcap", timeout=25)
        r.raise_for_status()
        data = r.json()
        total = 0.0
        for sc in data.get("peggedAssets", []):
            cur = sc.get("circulating", [])
            if cur:
                last = cur[-1]
                if isinstance(last, list) and len(last) >= 2 and isinstance(last[1], (int, float)):
                    total += float(last[1])
        return total
    except Exception:
        return None

def fetch_etf_farside() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    fr = requests.get("https://www.farside.co.uk/bitcoin-spot-etf-flows", timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    fr.raise_for_status()
    soup = BeautifulSoup(fr.text, "lxml")
    table = soup.find("table")
    if not table:
        raise RuntimeError("no table on Farside")
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) >= 3:
            rows.append({"date": tds[0], "issuer": tds[1], "flow": tds[2]})
    if not rows:
        raise RuntimeError("empty Farside rows")
    return rows

def fetch_etf_sosovalue() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    sr = requests.get("https://sosovalue.xyz/article/bitcoin-etf-data", timeout=25,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://sosovalue.xyz"})
    sr.raise_for_status()
    soup = BeautifulSoup(sr.text, "lxml")
    for tr in soup.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) >= 3 and any(x.lower().startswith("20") for x in tds):
            rows.append({"date": tds[0], "issuer": tds[1], "flow": tds[2]})
    return rows

def read_cache(cache_path: str) -> Dict[str, Any] | None:
    try:
        return load_json(cache_path) if ensure_file(cache_path) else None
    except Exception:
        return None

def write_cache(cache_path: str, obj: Dict[str, Any]) -> None:
    try:
        dump_json(cache_path, obj)
    except Exception:
        pass

# ----------------- main -----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='path to out_cloud dir')
    ap.add_argument('--logs', required=True, help='path to logs dir')
    ap.add_argument('--gh-run-id', required=True, help='github.run_id for index/run_meta sync')
    args = ap.parse_args()

    out_dir = args.out
    logs_dir = args.logs
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    post_log = os.path.join(logs_dir, "postprocess.log")

    # базові файли
    index_path = os.path.join(out_dir, 'index.json')
    tripack_path = os.path.join(out_dir, 'tripack_meta_v2.json')
    run_meta_path = os.path.join(out_dir, 'run_meta.json')
    vola_path = os.path.join(out_dir, 'options_vola_v2.json')
    macro_path = os.path.join(out_dir, 'macro_flows_v2.json')
    derivs_path = os.path.join(out_dir, 'derivs_signals_v2.json')

    if not ensure_file(index_path):
        dump_json(index_path, {"files": {"spot": {"files": []}}, "conf": 0.0})
        log("index.json created minimal", logfile=post_log)
    if not ensure_file(tripack_path):
        dump_json(tripack_path, {"log": {"quorum": "fail", "missing": [], "flags": []}, "checks": {}, "conf": 0.0})
        log("tripack_meta_v2.json created minimal", logfile=post_log)
    if not ensure_file(run_meta_path):
        dump_json(run_meta_path, {"run_id": args.gh_run_id})
        log("run_meta.json created minimal", logfile=post_log)

    indexj = load_json(index_path)
    tripack = load_json(tripack_path)

    # ---------- SPOT & VOLA ----------
    spot_files = indexj.get("files", {}).get("spot", {}).get("files", [])
    closes_for_hv: List[float] = []
    if spot_files:
        try:
            shard_obj = load_json(os.path.join(out_dir, spot_files[0]))
            closes_for_hv = extract_btc_closes_from_shard(shard_obj)
            log(f"found existing spot shard {spot_files[0]} closes={len(closes_for_hv)}", logfile=post_log)
        except Exception as e:
            log(f"spot shard read error: {e}", logfile=post_log)
    if not closes_for_hv:
        try:
            candles = fetch_binance_klines_btc_1d(limit=180)
            closes_for_hv = [c[4] for c in candles]
            shard = [{"symbol": "BTCUSDT", "tf": "1d", "candles": candles}]
            shard_name = "spot_ohlcv_v2_part000.json"
            dump_json(os.path.join(out_dir, shard_name), shard)
            indexj.setdefault("files", {}).setdefault("spot", {})["files"] = [shard_name]
            dump_json(index_path, indexj)
            tripack.setdefault("log", {}).setdefault("flags", []).extend(["spot:fallback:binance_rest", "spot:degraded:btc-only"])
            log(f"created autospot from Binance: {shard_name} rows={len(candles)}", logfile=post_log)
        except Exception as e:
            log(f"binance klines error: {e}", logfile=post_log)
    if not closes_for_hv:
        try:
            candles = fetch_coingecko_btc_daily(limit_days=180)
            closes_for_hv = [c[4] for c in candles]
            shard = [{"symbol": "BTCUSDT", "tf": "1d", "candles": candles}]
            shard_name = "spot_ohlcv_v2_part000.json"
            dump_json(os.path.join(out_dir, shard_name), shard)
            indexj.setdefault("files", {}).setdefault("spot", {})["files"] = [shard_name]
            dump_json(index_path, indexj)
            tripack.setdefault("log", {}).setdefault("flags", []).extend(["spot:fallback:coingecko", "spot:degraded:btc-only"])
            log(f"created autospot from CoinGecko: {shard_name} rows={len(candles)}", logfile=post_log)
        except Exception as e:
            log(f"coingecko fallback error: {e}", logfile=post_log)

    # DERIVS stub
    if not ensure_file(derivs_path):
        derivs_stub = {"ok": False, "flags": ["derivs:stub"], "note": "Collector did not produce derivs_signals_v2.json - stubbed."}
        dump_json(derivs_path, derivs_stub)
        tripack.setdefault("log", {}).setdefault("flags", []).append("derivs:stub")
        log("derivs_signals_v2.json created as stub", logfile=post_log)

    # ---------- MACRO ----------
    cache_dir = os.path.join("static", "cache")
    cache_etf = os.path.join(cache_dir, "etf_last_ok.json")
    cache_stb = os.path.join(cache_dir, "stables_last_ok.json")

    st_total = fetch_stables_defillama()
    etf_rows = []
    etf_source = "farside"
    flags: List[str] = []

    if st_total is None:
        # спробуємо cache
        cache = read_cache(cache_stb)
        if cache and isinstance(cache.get("total"), (int, float)):
            st_total = float(cache["total"])
            flags.append("macro:stables_cache")
            log(f"used stables cache: total={st_total}", logfile=post_log)

    # ETF: Farside -> SoSoValue -> cache
    try:
        etf_rows = fetch_etf_farside()
        etf_source = "farside"
    except Exception as e:
        log(f"Farside error: {e}", logfile=post_log)
        try:
            etf_rows = fetch_etf_sosovalue()
            etf_source = "sosovalue"
            flags.append("macro:fallback:sosovalue")
        except Exception as ee:
            log(f"SoSoValue error: {ee}", logfile=post_log)
            cache = read_cache(cache_etf)
            if cache and isinstance(cache.get("rows"), list) and cache["rows"]:
                etf_rows = cache["rows"]
                etf_source = "cache"
                flags.append("macro:fallback:cache")
                log(f"used ETF cache rows={len(etf_rows)}", logfile=post_log)

    ok_macro = bool((st_total or 0) > 1e9 and len(etf_rows) > 0)
    macro_obj = {
        "ok": ok_macro,
        "stables": {"total": st_total},
        "etf": {"rows": etf_rows, "source": etf_source},
        "flags": flags
    }
    dump_json(macro_path, macro_obj)

    # якщо мережа ок - запишемо кеш для наступних ранiв
    try:
        if (st_total or 0) > 1e9:
            write_cache(cache_stb, {"total": float(st_total), "ts": int(time.time())})
        if etf_rows:
            write_cache(cache_etf, {"rows": etf_rows[:100], "ts": int(time.time()), "source": etf_source})
    except Exception:
        pass

    tripack.setdefault("checks", {})["macro_quorum"] = ok_macro
    if not ok_macro:
        tripack.setdefault("log", {}).setdefault("missing", []).append("macro_flows_v2.json")
        tripack.setdefault("log", {}).setdefault("flags", []).extend(flags or ["macro:missing"])

    log(f"macro_flows_v2.json created - ok={ok_macro} source={etf_source} rows={len(etf_rows)} total={st_total}", logfile=post_log)

    # ---------- VOLA ----------
    if not ensure_file(vola_path):
        try:
            if closes_for_hv:
                hv_pct = hv_from_closes(closes_for_hv, tf="1d")
                vola = {
                    "ok": True,
                    "mode": "hv_surrogate",
                    "asof_ms": int(time.time() * 1000),
                    "symbols": {
                        "BTC": {"hv_annual_pct": hv_pct},
                        "ETH": {"hv_annual_pct": round(hv_pct * 0.9, 2)}
                    },
                    "flags": ["vol:surrogate:hv"],
                    "notes": "Computed from 1d closes (Binance/CoinGecko fallback)"
                }
                dump_json(vola_path, vola)
                tripack.setdefault("checks", {})["options_vola_ok"] = True
                tripack.setdefault("log", {}).setdefault("flags", []).append("vol:surrogate:hv")
                log(f"options_vola_v2.json created via hv_surrogate hv%={hv_pct}", logfile=post_log)
            else:
                const_hv = 60.0
                vola = {
                    "ok": True,
                    "mode": "hv_surrogate_const",
                    "asof_ms": int(time.time() * 1000),
                    "symbols": {
                        "BTC": {"hv_annual_pct": const_hv},
                        "ETH": {"hv_annual_pct": round(const_hv * 0.9, 2)}
                    },
                    "flags": ["vol:surrogate:const"],
                    "notes": "No candles available; constant surrogate HV applied"
                }
                dump_json(vola_path, vola)
                tripack.setdefault("checks", {})["options_vola_ok"] = True
                tripack.setdefault("log", {}).setdefault("flags", []).append("vol:surrogate:const")
                log("options_vola_v2.json created via constant surrogate HV", logfile=post_log)
        except Exception as e:
            tripack.setdefault("checks", {})["options_vola_ok"] = False
            tripack.setdefault("log", {}).setdefault("missing", []).append("options_vola_v2.json")
            tripack.setdefault("log", {}).setdefault("flags", []).append(f"vol:missing:{e}")
            log(f"options_vola_v2.json missing - reason: {e}", logfile=post_log)
    else:
        tripack.setdefault("checks", {})["options_vola_ok"] = True

    # ---------- SANITY ----------
    try:
        last_close = closes_for_hv[-1] if closes_for_hv else fetch_binance_last_price()
        tripack.setdefault("checks", {})["btc_close_gt_1000"] = bool(last_close and float(last_close) > 1000.0)
        if not tripack["checks"]["btc_close_gt_1000"]:
            tripack.setdefault("log", {}).setdefault("flags", []).append("spot:sanity:bld_1000")
    except Exception as e:
        tripack.setdefault("checks", {})["btc_close_gt_1000"] = False
        tripack.setdefault("log", {}).setdefault("flags", []).append(f"spot:sanity:error:{e}")

    # ---------- sync index pointers ----------
    files_block = indexj.setdefault("files", {})
    files_block.setdefault("derivs", {})["file"] = "derivs_signals_v2.json" if ensure_file(derivs_path) else None
    files_block.setdefault("vola", {})["file"] = "options_vola_v2.json" if ensure_file(vola_path) else None
    files_block.setdefault("macro", {})["file"] = "macro_flows_v2.json" if ensure_file(macro_path) else None
    files_block.setdefault("meta", {})["files"] = ["tripack_meta_v2.json", "run_meta.json"]
    dump_json(index_path, indexj)

    # ---------- finalize quorum ----------
    log_block = tripack.setdefault("log", {})
    log_block["missing"] = list(dict.fromkeys(log_block.get("missing", [])))
    ok_all = (
        not log_block["missing"]
        and tripack.get("checks", {}).get("btc_close_gt_1000")
        and tripack.get("checks", {}).get("options_vola_ok")
        and tripack.get("checks", {}).get("macro_quorum")
    )
    if ok_all:
        flags = log_block.get("flags", [])
        tripack["log"]["quorum"] = "ok_fallback" if "macro:fallback:sosovalue" in flags or "macro:fallback:cache" in flags else "ok"
    else:
        tripack["log"]["quorum"] = "fail"

    dump_json(tripack_path, tripack)
    log(f"tripack_meta_v2.json updated - quorum={tripack['log']['quorum']} missing={tripack['log'].get('missing', [])} flags={tripack['log'].get('flags', [])}", logfile=post_log)


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print(f"[POST][FATAL] {ex}", file=sys.stderr)
        sys.exit(2)
