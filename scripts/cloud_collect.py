# scripts/cloud_collect.py
import os, json, time, pathlib

OUT = pathlib.Path("out"); LOG = pathlib.Path("log")
OUT.mkdir(exist_ok=True); LOG.mkdir(exist_ok=True)
ts = time.strftime("%Y-%m-%d %H:%M:%S")

def w(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

# Мінімально коректні файли контракту (поки заглушки, але «правильні»)
w("spot_ohlcv_v2.json",       {"ok": True, "pairs": [], "note":"stub", "ts": ts})
w("derivs_signals_v2.json",   {"ok": True, "funding": [], "oi": [], "basis": [], "ts": ts})
w("options_vola_v2.json",     {"ok": True, "dvol": {"BTC": None, "ETH": None}, "source":"stub", "ts": ts})
w("macro_flows_v2.json",      {"ok": True, "stables": {"total": 0, "delta_7d": 0}, "etf":{"rows":[]}, "ts": ts})
w("tripack_meta_v2.json",     {
    "breadth_meta": {}, "policy_flags": [],
    "conf": 1.00,
    "log": {"quorum":"ok_fallback","missing":[],"flags":["stub-cloud"], "summary":"init stub"},
    "ts": ts
})
(LOG / "cloud.txt").write_text("cloud stub ok\n", encoding="utf-8")
print("cloud_collect stub ok")
