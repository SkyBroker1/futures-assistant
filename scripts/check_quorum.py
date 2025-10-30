#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, json, os, sys, glob

OK, FAIL = 0, 1

def load_json(path):
    with open(path,'r',encoding='utf-8') as f: return json.load(f)

def eprint(*a): print(*a, file=sys.stderr)

def ls_dir(path):
    try:
        for p in sorted(glob.glob(os.path.join(path,"*"))):
            sz = os.path.getsize(p) if os.path.isfile(p) else -1
            print(f"[LS] {os.path.basename(p)} size={sz}")
    except Exception as e:
        eprint(f"[LS][ERR] {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True); ap.add_argument('--logs', required=True)
    args = ap.parse_args()
    out_dir = args.out
    must = ['tripack_meta_v2.json','index.json','derivs_signals_v2.json','options_vola_v2.json','macro_flows_v2.json','run_meta.json']
    missing = [m for m in must if not os.path.isfile(os.path.join(out_dir,m))]
    if missing:
        eprint(f"[QUORUM] missing files: {missing}")
        print("[DEBUG] OUT_DIR listing:"); ls_dir(out_dir)
        if os.path.isfile(os.path.join(out_dir,'index.json')):
            try: print("[DEBUG] index.json content:"); print(json.dumps(load_json(os.path.join(out_dir,'index.json')),ensure_ascii=False,indent=2))
            except Exception as e: eprint(f"[DEBUG] index.json read err: {e}")
        return FAIL
    tripack = load_json(os.path.join(out_dir,'tripack_meta_v2.json'))
    checks = tripack.get('checks',{})
    if checks.get('btc_close_gt_1000') is False: eprint("[SANITY] BTCUSDT.last_close<=1000"); return FAIL
    if checks.get('macro_quorum') is False: eprint("[SANITY] macro stables.total<=1e9 або etf.rows==0"); return FAIL
    if checks.get('options_vola_ok') is False: eprint("[SANITY] options_vola_v2.ok==false"); return FAIL
    quorum = tripack.get('log',{}).get('quorum','fail')
    if quorum not in ('ok','ok_fallback','ok_seed'):
        eprint(f"[QUORUM] status '{quorum}' invalid, missing={tripack.get('log',{}).get('missing',[])}, flags={tripack.get('log',{}).get('flags',[])}"); 
        return FAIL
    print("[OK] quorum and sanity passed"); return OK

if __name__ == '__main__': sys.exit(main())
