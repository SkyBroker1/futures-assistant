#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
postprocess_fill_missing.py
- Добиває відсутні JSON після collector:
  * spot_ohlcv_v2_part000.json: мінімальний fallback з Binance REST (BTCUSDT 1d), якщо spot відсутній
  * options_vola_v2.json: hv_surrogate на основі BTCUSDT
  * macro_flows_v2.json: DeFiLlama + Farside -> SoSoValue фолбек
  * derivs_signals_v2.json: безпечний stub, якщо collector його не створив
- Синхронізує index.json з фактично наявними файлами
- Оновлює tripack_meta_v2.json: checks, log.flags, log.missing, quorum
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

# ---------- util ----------

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

# ---------- spot minimal fallback ----------

def ensure_minimal_spot(out_dir: str, indexj: Dict[str, Any], tripack: Dict[str, Any], logf: str | None = None) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    """
    Якщо у index.json немає жодного spot-шарду - створює мінімальний:
      - завантажує BTCUSDT 1d з Binance REST (limit=120)
      - зберігає як out_dir/spot_ohlcv_v2_part000.json у форматі списку записів:
            [{"symbol":"BTCUSDT","tf":"1d","candles":[[ts,open,high,low,close,vol], ...]}]
      - додає файл у index.json.files.spot.files
      - додає прапор 'spot:minimal_fallback' у tripack.log.flags
    Повертає оновлені indexj, tripack, а також last_close (float, 0.0 якщо не вдалось).
    """
    files_spot = indexj.setdefault("files", {}).setdefault("spot", {}).setdefault("files", [])
    if files_spot:
        # вже існує - нічого не робимо, але спробуємо прочитати last_close
        try:
            shard_path = os.path.join(out_dir, files_spot[0])
            data = load_json(shard_path)
            closes, _tf = _select_btc_series_from_spot(data)
            last_close = float(closes[-1]) if closes else 0.0
            return indexj, tripack, last_close
        except Exception:
            return indexj, tripack, 0.0

    # немає spot - тягнемо мінімальний з Binance (публічний)
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1d", "limit": 120}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        kl = r.json()
        candles = []
        # Binance kline: [openTime,open,high,low,close,volume,closeTime,...]
        for row in kl:
            ts = int(row[0])
            o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4]); v = float(row[5])
            candles.append([ts, o, h, l, c, v])
        shard = [{
            "symbol": "BTCUSDT",
            "tf": "1d",
            "candles": candles
        }]
        shard_name = "spot_ohlcv_v2_part000.json"
        dump_json(os.path.join(out_dir, shard_name), shard)
        files_spot.append(shard_name)
        log("spot minimal shard created via Binance REST",
