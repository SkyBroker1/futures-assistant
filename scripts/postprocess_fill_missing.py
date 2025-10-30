#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
postprocess_fill_missing.py v1.4
Мета - гарантувати файли для кворуму навіть якщо collector впав:
- SPOT autospot: тягнемо BTCUSDT 1d з Binance REST (публічно), пишемо spot_ohlcv_v2_part000.json,
  синхронізуємо index.json.files.spot.files.
- VOLA: рахуємо hv_surrogate з отриманих свічок, пишемо options_vola_v2.json.
- MACRO: DeFiLlama + Farside -> SoSoValue, пишемо macro_flows_v2.json.
- DERIVS: якщо collector не створив - пишемо безпечний stub з ок=false.
- Оновлюємо tripack_meta_v2.json (checks, log.missing, log.flags, log.quorum) та index.json meta-вказівники.
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


# ----------------- helpers -----------------

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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ----------------- spot autoload (REST) -----------------

def fetch_binance_klines_btc_1d(limit: int = 180) -> List[List[float]]:
    url = "https://api.binance.com/api/v3/klines"
    qs = {"symbol": "BTCUSDT", "interval": "1d", "limit": str(limit)}
    r = requests.get(url, params=qs, timeout=25)
    r.raise_for_status()
    data = r.json()
    candles = []
    for k in data:
        # [open_time, open, high, low, close, volume, close_time, ...]
        ts = int(k[0])
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); v = float(k[5])
        candles.append([ts, o, h, l, c, v])
    if len(candles) < 30:
        raise RuntimeError(f"too few candles from REST: {len(candles)}")
    return candles

def ensure_spot_and_index(out_dir: str, indexj: Dict[str, Any], logf: str | None = None) -> Tuple[Dict[str, Any], List[List[float]], bool]:
    """
    Якщо немає жодного spot-шарда - створюємо part000 з BTCUSDT 1d із REST.
    Повертає: оновлений index, список свічок BTCUSDT 1d (для HV), прапор created.
    Якщо шард уже є - читаємо його, повертаємо свічки для HV.
    """
    files_spot = indexj.get("files", {}).get("spot", {}).get("files", [])
    if files_spot:
        # пробуємо прочитати перший шард і витягнути BTCUSDT 1d
        shard_path = os.path.join(out_dir, files_spot[0])
        if ensure_file(shard_path):
            shard = load_json(shard_path)
            closes = _extract_btc_closes(shard)
            if closes:
                log(f"spot shard already present: {files_spot[0]} (closes={len(closes)})", logfile=logf)
                return indexj, closes, False
            else:
                log("existing spot shard found but no BTCUSDT 1d - will create autospot as degraded", logfile=logf)

    # створюємо autospot
    candles = fetch_binance_klines_btc_1d(limit=180)
    shard = [{"symbol": "BTCUSDT", "tf": "1d", "candles": candles}]
    shard_name = "spot_ohlcv_v2_part000.json"
    shard_path = os.path.join(out_dir, shard_name)
    dump_json(shard_path, shard)

    files = indexj.setdefault("files", {})
    files.setdefault("spot", {})["files"] = [shard_name]
    dump_json(os.path.join(out_dir, "index.json"), indexj)

    log(f"created autospot shard: {shard_name} rows={len(candles)}", logfile=logf)
    return indexj, candles, True


# ----------------- HV from closes -----------------

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


def _extract_btc_closes(spot_obj: Any) -> List[float]:
    # підтримка форматів: список записів або dict з rows/data
    def extract_from_rec(rec: Dict[str, Any]) -> List[float] | None:
        if rec.get("symbol") == "BTCUSDT" and rec.get("tf") in ("1d", "4h"):
            candles = rec.get("candles") or rec.get("ohlcv") or []
            closes = [c[4] for c in candles if isinstance(c, (list, tuple)) and len(c) >= 5]
            return closes if closes else None
        return None

    if isinstance(spot_obj, list):
        for rec in spot_obj:
            if isinstance(rec, dict):
                closes = extract_from_rec(rec)
                if closes:
                    return closes
    if isinstance(spot_obj, dict):
        rows = spot_obj.get("rows") or spot_obj.get("data") or []
        for rec in rows:
            if isinstance(rec, dict):
                closes = extract_from_rec(rec)
                if closes:
                    return closes
    return []


# ----------------- macro fetch -----------------

def fetch_stables_and_etf(logf: str | None = None) -> Dict[str, Any]:
    stables_total = None
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
        stables_total = total
        log(f"DeFiLlama stables total parsed: {round(stables_total or 0, 2)}", logfile=logf)
    except Exception as e:
        log(f"DeFiLlama fetch error: {e}", logfile=logf)
        stables_total = None

    etf_rows: List[Dict[str, Any]] = []
    etf_source = "farside"
    try:
        fr = requests.get("https://www.farside.co.uk/bitcoin-spot-etf-flows", timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        fr.raise_for_status()
        soup = BeautifulSoup(fr.text, "lxml")
        table = soup.find("table")
        if not table:
            raise RuntimeError("no table on Farside")
        for tr in table.find_all("tr")[1:]:
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 3:
                etf_rows.append({"date": tds[0], "issuer": tds[1], "flow": tds[2]})
        if not etf_rows:
            raise RuntimeError("empty Farside rows")
        log(f"Farside parsed rows: {len(etf_rows)}", logfile=logf)
    except Exception as e:
        log(f"Farside error: {e} - switching to SoSoValue fallback", logfile=logf)
        etf_source = "sosovalue"
        etf_rows = []
        try:
            sr = requests.get("https://sosovalue.xyz/article/bitcoin-etf-data", timeout=25, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://sosovalue.xyz"})
            sr.raise_for_status()
            soup = BeautifulSoup(sr.text, "lxml")
            for tr in soup.find_all("tr"):
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) >= 3 and any(x.lower().startswith("20") for x in tds):
                    etf_rows.append({"date": tds[0], "issuer": tds[1], "flow": tds[2]})
            log(f"SoSoValue parsed rows: {len(etf_rows)}", logfile=logf)
        except Exception as ee:
            log(f"SoSoValue error: {ee}", logfile=logf)
            etf_rows = []

    return {"stables": {"total": stables_total}, "etf": {"rows": etf_rows[:50], "source": etf_source}}


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

    # 1) SPOT + closes для HV
    created_autospot = False
    closes_for_hv: List[float] = []
    try:
        indexj, candles, created = ensure_spot_and_index(out_dir, indexj, logf=post_log)
        created_autospot = created
        closes_for_hv = [c[4] for c in candles]
        if created_autospot:
            tripack.setdefault("log", {}).setdefault("flags", []).extend(["spot:fallback:binance_rest", "spot:degraded:btc-only"])
    except Exception as e:
        tripack.setdefault("log", {}).setdefault("missing", []).append("spot_ohlcv_v2_partNNN.json")
        tripack.setdefault("log", {}).setdefault("flags", []).append(f"spot:fallback_failed:{e}")
        closes_for_hv = []

    # 2) DERIVS stub якщо відсутній
    if not ensure_file(derivs_path):
        derivs_stub = {"ok": False, "flags": ["derivs:stub"], "note": "Collector did not produce derivs_signals_v2.json - stubbed."}
        dump_json(derivs_path, derivs_stub)
        tripack.setdefault("log", {}).setdefault("flags", []).append("derivs:stub")
        log("derivs_signals_v2.json created as stub", logfile=post_log)

    # 3) MACRO
    if not ensure_file(macro_path):
        macro = fetch_stables_and_etf(logf=post_log)
        flags: List[str] = []
        ok = True
        st_total = macro.get("stables", {}).get("total")
        if st_total is None:
            ok = False; flags.append("macro:stables_missing")
        etf_rows = macro.get("etf", {}).get("rows", [])
        etf_source = macro.get("etf", {}).get("source", "unknown")
        if not etf_rows:
            ok = False; flags.append("macro:etf_missing")
        elif etf_source == "sosovalue":
            flags.append("macro:fallback:sosovalue")

        out_obj = {"ok": ok, "stables": {"total": st_total}, "etf": {"rows": etf_rows, "source": etf_source}, "flags": flags}
        dump_json(macro_path, out_obj)
        tripack.setdefault("checks", {})["macro_quorum"] = bool(ok and (st_total or 0) > 1e9 and len(etf_rows) > 0)
        if not tripack["checks"]["macro_quorum"]:
            tripack.setdefault("log", {}).setdefault("missing", []).append("macro_flows_v2.json")
            tripack.setdefault("log", {}).setdefault("flags", []).extend(flags)
        log(f"macro_flows_v2.json created - ok={out_obj['ok']} source={etf_source} rows={len(etf_rows)}", logfile=post_log)
    else:
        try:
            mm = load_json(macro_path)
            st_total = (mm.get("stables") or {}).get("total")
            etf_rows = (mm.get("etf") or {}).get("rows") or []
            tripack.setdefault("checks", {})["macro_quorum"] = bool((st_total or 0) > 1e9 and len(etf_rows) > 0)
        except Exception:
            tripack.setdefault("checks", {})["macro_quorum"] = False

    # 4) VOLA з closes_for_hv
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
                    "notes": "Computed from Binance REST BTCUSDT 1d closes"
                }
                dump_json(vola_path, vola)
                tripack.setdefault("checks", {})["options_vola_ok"] = True
                tripack.setdefault("log", {}).setdefault("flags", []).append("vol:surrogate:hv")
                log(f"options_vola_v2.json created via hv_surrogate hv%={hv_pct}", logfile=post_log)
            else:
                raise RuntimeError("no closes for HV")
        except Exception as e:
            tripack.setdefault("checks", {})["options_vola_ok"] = False
            tripack.setdefault("log", {}).setdefault("missing", []).append("options_vola_v2.json")
            tripack.setdefault("log", {}).setdefault("flags", []).append(f"vol:missing:{e}")
            log(f"options_vola_v2.json missing - reason: {e}", logfile=post_log)
    else:
        tripack.setdefault("checks", {})["options_vola_ok"] = True

    # 5) SANITY: btc_close_gt_1000
    try:
        last_close = closes_for_hv[-1] if closes_for_hv else 0.0
        tripack.setdefault("checks", {})["btc_close_gt_1000"] = bool(last_close and last_close > 1000.0)
        if not tripack["checks"]["btc_close_gt_1000"]:
            tripack.setdefault("log", {}).setdefault("flags", []).append("spot:sanity:bld_1000")
    except Exception as e:
        tripack.setdefault("checks", {})["btc_close_gt_1000"] = False
        tripack.setdefault("log", {}).setdefault("flags", []).append(f"spot:sanity:error:{e}")

    # 6) sync index meta pointers
    files_block = indexj.setdefault("files", {})
    files_block.setdefault("derivs", {})["file"] = "derivs_signals_v2.json" if ensure_file(derivs_path) else None
    files_block.setdefault("vola", {})["file"] = "options_vola_v2.json" if ensure_file(vola_path) else None
    files_block.setdefault("macro", {})["file"] = "macro_flows_v2.json" if ensure_file(macro_path) else None
    files_block.setdefault("meta", {})["files"] = ["tripack_meta_v2.json", "run_meta.json"]
    dump_json(index_path, indexj)

    # 7) finalize quorum
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
        tripack["log"]["quorum"] = "ok_fallback" if "macro:fallback:sosovalue" in flags else "ok"
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
