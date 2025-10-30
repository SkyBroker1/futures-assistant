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

    missing_hard = []
    for p in [tripack_path, index_path]:
        if not os.path.isfile(p):
            missing_hard.append(p)
    if missing_hard:
        eprint(f"[QUORUM] hard-missing files: {missing_hard}")
        return FAIL

    tripack = load_json(tripack_path)
    indexj = load_json(index_path)

    # базові існування
    need_files = [
        'derivs_signals_v2.json',
        'options_vola_v2.json',
        'macro_flows_v2.json',
        'run_meta.json'
    ]
    for nf in need_files:
        if not os.path.isfile(os.path.join(out_dir, nf)):
            missing_hard.append(nf)

    # spot шардінг - принаймні 1 файл
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

    # логіка кворуму згідно правил
    log = tripack.get('log', {})
    quorum = log.get('quorum', 'fail')
    log_missing = log.get('missing', [])
    flags = log.get('flags', [])

    # sanity на BTC close - читаємо з tripack.summary якщо доступно
    sanity_ok = tripack.get('checks', {}).get('btc_close_gt_1000', None)
    if sanity_ok is False:
        eprint("[SANITY] BTCUSDT.last_close<=1000")
        return FAIL

    # macro квоти
    macro_ok = tripack.get('checks', {}).get('macro_quorum', None)
    if macro_ok is False:
        eprint("[SANITY] macro stables.total<=1e9 або etf.rows==0")
        return FAIL

    # vola ok
    vola_ok = tripack.get('checks', {}).get('options_vola_ok', None)
    if vola_ok is False:
        eprint("[SANITY] options_vola_v2.ok==false")
        return FAIL

    # фінальне рішення
    if quorum not in ('ok', 'ok_fallback'):
        eprint(f"[QUORUM] status '{quorum}' invalid, missing={log_missing}, flags={flags}")
        return FAIL

    print("[OK] quorum and sanity passed")
    return OK

if __name__ == '__main__':
    sys.exit(main())
