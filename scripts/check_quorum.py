#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, sys

OK = 0
FAIL = 1

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def eprint(*a):
    print(*a, file=sys.stderr)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True, help='path to out_cloud dir')
    ap.add_argument('--logs', required=True, help='path to logs dir')
    args = ap.parse_args()

    out_dir = args.out
    tripack_path = os.path.join(out_dir, 'tripack_meta_v2.json')
    index_path = os.path.join(out_dir, 'index.json')

    # 0) базові файли
    missing_hard = []
    for p in [tripack_path, index_path]:
        if not os.path.isfile(p):
            missing_hard.append(os.path.basename(p))
    if missing_hard:
        eprint(f"[QUORUM] hard-missing: {missing_hard}")
        return FAIL

    tripack = load_json(tripack_path)
    indexj = load_json(index_path)

    # 1) наявність ключових JSON
    need_files = [
        'derivs_signals_v2.json',
        'options_vola_v2.json',
        'macro_flows_v2.json',
        'run_meta.json'
    ]
    for nf in need_files:
        if not os.path.isfile(os.path.join(out_dir, nf)):
            missing_hard.append(nf)

    # 2) spot - принаймні один шард
    spot_files = []
    try:
        spot_files = indexj.get('files', {}).get('spot', {}).get('files', [])
    except Exception:
        pass
    if not spot_files:
        missing_hard.append('spot_ohlcv_v2_partNNN.json')

    if missing_hard:
        eprint(f"[QUORUM] missing files: {missing_hard}")
        return FAIL

    # 3) sanity checks з tripack
    checks = tripack.get('checks', {})
    if checks.get('btc_close_gt_1000') is False:
        eprint("[SANITY] BTCUSDT.last_close<=1000")
        return FAIL
    if checks.get('macro_quorum') is False:
        eprint("[SANITY] macro stables.total<=1e9 або etf.rows==0")
        return FAIL
    if checks.get('options_vola_ok') is False:
        eprint("[SANITY] options_vola_v2.ok==fal
