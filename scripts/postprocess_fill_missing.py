#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
postprocess_fill_missing.py v1.7.1
- Фікс SyntaxError у extract_btc_closes_from_shard (жодних compound-стейтментів після ';').
- Логіка фолбеків і seed як у v1.7 (див. коментарі нижче).
"""

import argparse, json, os, sys, time, math, statistics
from typing import Any, Dict, List
import requests
from bs4 import BeautifulSoup

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
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# ----------------- external fetchers -----------------
def fetch_binance_klines_btc_1d(limit: int = 180) -> List[List[float]]:
    url = "https://api.binance.com/api/v3/klines"
    qs = {"symbol": "BTCUSDT", "interval": "1d", "limit": str(limit)}
    r = requests.get(url, params=qs, timeout=25); r.raise_for_status()
    data = r.json()
    out: List[List[float]] = []
    for k in data:
        ts = int(k[0]); o=float(k[1]); h=float(k[2]); l=float(k[3]); c=float(k[4]); v=float(k[5])
        out.append([ts,o,h,l,c,v])
    if len(out) < 30:
        raise RuntimeError(f"too few candles from Binance: {len(out)}")
    return out

def fetch_coingecko_btc_daily(limit_days: int = 180) -> List[List[fl]()]()
