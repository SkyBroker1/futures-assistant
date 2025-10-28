import json, os, argparse, time, pathlib

parser = argparse.ArgumentParser()
parser.add_argument("--out", required=True)
args = parser.parse_args()

out_dir = pathlib.Path(args.out)
out_dir.mkdir(parents=True, exist_ok=True)
ts = time.strftime("%Y%m%d_%H%M")

# Тут поки заглушка під локальні дані (напр., ліквідації з WS)
# Замінимо її на реальний збір, коли підключимо твій self-hosted/локальний збирач
data = {
    "ok": True,
    "count": 0,
    "note": "local stub - replace with real WS/liquidations collector",
    "ts": ts
}
(out_dir / f"derivs_ws_liq_v2_{ts}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
print("local ok")
