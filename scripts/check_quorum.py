#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, sys, glob, subprocess

OK = 0
FAIL = 1

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def eprint(*a):
    print(*a, file=sys.stderr)

def ls_dir(path):
    try:
        for p in sorted(glob.glob(os.path.join(path, "*"))):
            sz = os.path.getsize(p) if os.path.isfile(p) else -1
            print(f"[LS] {os.path.basename(p)} size={sz}")
    except Exception as e:
        eprint(f"[LS][ERR] {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='path to out_cloud dir')
    ap.add_argument('--logs', required=True, help='path to logs dir')
    args = ap.parse_args()

    out_dir = args.out
    tripack_path = os.path.join(out_dir, 'tripack_meta_v2.json')
    index_path = os.path.join(out_dir, 'index.json')

    # базові файли
    missing_hard = []
    for p in [tripack_path, index_path]:
        if not os.path.isfile(p):
            missing_hard.append(os.path.basename(p))
    if missing_hard:
        eprint(f"[QUORUM] hard-missing: {missing_hard}")
        ls_dir(out_dir)
        return FAIL

    tripack = load_json(tripack_path)
    indexj = load_json(index_path)

    need_files = [
        'derivs_signals_v2.json',
        'options_vola_v2.json',
        'macro_flows_v2.json',
        'run_meta.json'
    ]
    for nf in need_files:
        if not os.path.isfile(os.path.join(out_dir, nf)):
            missing_hard.append(nf)

    spot_files = []
    try:
        spot_files = indexj.get('files', {}).get('spot', {}).get('files', [])
    except Exception:
        pass
    if not spot_files:
        missing_hard.append('spot_ohlcv_v2_partNNN.json')

    if missing_hard:
        eprint(f"[QUORUM] missing files: {missing_hard}")
        print("[DEBUG] OUT_DIR listing:")
        ls_dir(out_dir)
        try:
            print("[DEBUG] index.json content:")
            print(json.dumps(indexj, ensure_ascii=False, indent=2))
        except Exception as e:
            eprint(f"[DEBUG] cannot print index.json: {e}")
        return FAIL

    checks = tripack.get('checks', {})
    if checks.get('btc_close_gt_1000') is False:
        eprint("[SANITY] BTCUSDT.last_close<=1000")
        return FAIL
    if checks.get('macro_quorum') is False:
        eprint("[SANITY] macro stables.total<=1e9 або etf.rows==0")
        return FAIL
    if checks.get('options_vola_ok') is False:
        eprint("[SANITY] options_vola_v2.ok==false")
        return FAIL

    log = tripack.get('log', {})
    quorum = log.get('quorum', 'fail')
    if quorum not in ('ok', 'ok_fallback'):
        eprint(f"[QUORUM] status '{quorum}' invalid, missing={log.get('missing', [])}, flags={log.get('flags', [])}")
        return FAIL

    print("[OK] quorum and sanity passed")
    return OK

if __name__ == '__main__':
    sys.exit(main())
