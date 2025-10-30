import os, json, time, math, pathlib, traceback
from datetime import datetime, timezone
import requests

OUT_DIR = pathlib.Path(os.getenv("OUT_DIR", "out"))
LOG_DIR = pathlib.Path(os.getenv("LOG_DIR", "log"))
MAX_JSON_MB = float(os.getenv("MAX_JSON_MB", "2.5"))

OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

def _write(path: pathlib.Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(path)

def _log_err(msg):
    (LOG_DIR / "cloud.txt").write_text(f"[{datetime.now(timezone.utc).isoformat()}] {msg}\n", encoding="utf-8")

# ---------- helpers ----------
def fetch_cg_btc_last():
    # простий fallback до CoinGecko
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    return float(data["prices"][-1][1])

def btc_close_from_spot_shard(spot_obj):
    """
    Підтримує 2 формати:
     1) [{"symbol":"BTCUSDT","tf":"1d","values":[[ts,open,high,low,close,vol], ...]}, ...]
     2) [{"id":"bitcoin","symbol":"BTC","tf":"1d","values":[...]}]
    """
    if isinstance(spot_obj, dict) and "data" in spot_obj:
        items = spot_obj["data"]
    elif isinstance(spot_obj, list):
        items = spot_obj
    else:
        return None
    cand = None
    for it in items:
        sym = (it.get("symbol") or it.get("id") or "").lower()
        if sym in ("btcusdt","bitcoin","btc"):
            vals = it.get("values") or []
            if vals:
                cand = float(vals[-1][4])
                break
    return cand

def calc_conf_penalties(meta):
    penalties = 0.0
    flags = meta["log"].setdefault("flags", [])

    # VOL
    try:
        vola = json.loads((OUT_DIR/"options_vola_v2.json").read_text())
        if vola.get("source") == "hv_surrogate":
            penalties += 0.10; flags.append("penalty:vol=-0.10:hv_surrogate")
        elif vola.get("source") == "volmex":
            penalties += 0.05; flags.append("penalty:vol=-0.05:fallback_volmex")
    except Exception:
        flags.append("vol:missing"); penalties += 0.10

    # ETF
    try:
        macro = json.loads((OUT_DIR/"macro_flows_v2.json").read_text())
        etf = macro.get("etf", {})
        if etf.get("rows", 0) <= 0:
            penalties += 0.01; flags.append("penalty:etf=-0.01:null")
        if etf.get("source") == "sosovalue":
            penalties += 0.05; flags.append("penalty:etf=-0.05:fallback_sosovalue")
    except Exception:
        flags.append("macro:missing"); penalties += 0.05

    return penalties, flags

def build_index(conf, policy_flags):
    items = {
        "files": {
            "spot": {"files": sorted([p.name for p in OUT_DIR.glob("spot_ohlcv_v2_part*.json")])},
            "derivs": {"file": "derivs_signals_v2.json"},
            "vola": {"file": "options_vola_v2.json"},
            "macro": {"file": "macro_flows_v2.json"},
        }
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "conf": round(conf, 2),
        "policy_flags": policy_flags
    }

def main():
    meta = {"log":{"flags":[], "missing":[]}, "checks":{}}

    # --- тут ваш існуючий збір 5 JSON ---
    # очікується, що до цього моменту вже створені:
    # - spot_ohlcv_v2_part001.json (мінімум з BTC та ETH)
    # - derivs_signals_v2.json
    # - options_vola_v2.json
    # - macro_flows_v2.json
    # - tripack_meta_v2.json (ми перезапишемо нижче акуратніше)
    # Якщо збір робите тут - залиште як є.

    # Sanity: BTC close > 1000
    btc_close = None
    # пробуємо з першого шарду
    try:
        spot_part1 = json.loads((OUT_DIR/"spot_ohlcv_v2_part001.json").read_text())
        btc_close = btc_close_from_spot_shard(spot_part1)
    except Exception:
        meta["log"]["flags"].append("spot_part001:missing")

    if btc_close is None:
        # пробуємо ще кілька шард (на майбутнє)
        for p in sorted(OUT_DIR.glob("spot_ohlcv_v2_part*.json")):
            try:
                data = json.loads(p.read_text())
                btc_close = btc_close_from_spot_shard(data)
                if btc_close is not None:
                    break
            except Exception:
                continue

    if btc_close is None:
        # останній шанс - CoinGecko
        try:
            btc_close = fetch_cg_btc_last()
            meta["log"]["flags"].append("sanity:btc_from_cg_fallback")
        except Exception:
            meta["log"]["flags"].append("sanity:btc_missing")

    meta["checks"]["btc_close"] = btc_close if btc_close is not None else "null"
    meta["checks"]["btc_close_gt_1000"] = bool(btc_close and btc_close > 1000)

    # Macro strict checks
    macro_ok = False
    try:
        macro = json.loads((OUT_DIR/"macro_flows_v2.json").read_text())
        st = float(macro["stables"]["total"])
        rows = int(macro["etf"]["rows"])
        macro_ok = (st > 1e9) and (rows > 0)
    except Exception:
        meta["log"]["missing"].append("macro_flows_v2.json")

    # VOL ok
    vola_ok = False
    try:
        vola = json.loads((OUT_DIR/"options_vola_v2.json").read_text())
        vola_ok = bool(vola.get("ok", False))
    except Exception:
        meta["log"]["missing"].append("options_vola_v2.json")

    # базова впевненість
    conf = 0.95
    # санкції
    penalties, _ = calc_conf_penalties(meta)
    conf -= penalties

    # quorum
    if meta["checks"]["btc_close_gt_1000"] and macro_ok and vola_ok:
        quorum = "ok" if penalties == 0 else "ok_fallback"
    else:
        quorum = "fail"
        conf = max(0.35, conf - 0.10)
        if not meta["checks"]["btc_close_gt_1000"]:
            meta["log"]["flags"].append("sanity:btc_close_le_1000_or_missing")

    # policy flags (простий дзеркальний вивід)
    policy_flags = {"leader-lock":"OFF","flow-alignment":"UNC","mHedge":"UNC"}

    # tripack_meta_v2.json
    tripack = {
        "breadth_meta": {},
        "policy_flags": policy_flags,
        "conf": round(conf, 2),
        "log": {
            "quorum": quorum,
            "missing": meta["log"]["missing"],
            "flags": meta["log"]["flags"]
        }
    }
    _write(OUT_DIR/"tripack_meta_v2.json", tripack)

    # index.json
    _write(OUT_DIR/"index.json", build_index(conf, policy_flags))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log_err(f"meta:macro {e}")
        # мінімальний аварійний вивід, щоб workflow не падав без файлів
        F = OUT_DIR
        for name, obj in {
            "tripack_meta_v2.json":{
                "breadth_meta":{}, "policy_flags":{"leader-lock":"UNC","flow-alignment":"UNC","mHedge":"UNC"},
                "conf":0.6, "log":{"quorum":"fail","missing":["exception"],"flags":[f"exception:{type(e).__name__}"]}
            },
            "index.json": {"generated_at": datetime.now(timezone.utc).isoformat(),"items":{"files":{}}, "conf":0.6, "policy_flags":{"leader-lock":"UNC","flow-alignment":"UNC","mHedge":"UNC"}}
        }.items():
            try: _write(F/name, obj)
            except: pass
        raise
