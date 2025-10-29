diff --git a/requirements.txt b/requirements.txt
index 5c13ac1..a9b7db2 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,3 +1,7 @@
 requests
 numpy
 pandas
+beautifulsoup4
+lxml
+
+# опційно: orjson для швидкого JSON (не обов'язково)
diff --git a/.github/workflows/run_futures_assistant.yml b/.github/workflows/run_futures_assistant.yml
index 3a21b89..e6c22d0 100644
--- a/.github/workflows/run_futures_assistant.yml
+++ b/.github/workflows/run_futures_assistant.yml
@@ -52,6 +52,17 @@ jobs:
         python scripts/cloud_collect.py
         echo "done"
 
+    - name: Echo health to logs
+      run: |
+        echo "== HEALTH INDEX ==" >> logs/cloud.txt || true
+        if [ -f out/index.json ]; then
+          head -c 2000 out/index.json >> logs/cloud.txt
+          echo "" >> logs/cloud.txt
+        fi
+        if [ -f out/tripack_meta_v2.json ]; then
+          head -c 4000 out/tripack_meta_v2.json >> logs/cloud.txt
+          echo "" >> logs/cloud.txt
+        fi
     - name: Upload logs
       uses: actions/upload-artifact@v4
       with:
diff --git a/scripts/cloud_collect.py b/scripts/cloud_collect.py
index 2b21f01..7f90a10 100644
--- a/scripts/cloud_collect.py
+++ b/scripts/cloud_collect.py
@@ -1,25 +1,55 @@
-# v0.3.1 - minimal 5 JSON
+# v0.4.0 - expanded data, fallbacks, sharding, policy flags
 import os, json, time, math, gzip, io
 from datetime import datetime, timedelta, timezone
 import requests
 import pandas as pd
+from bs4 import BeautifulSoup
 
 OUT_DIR = os.environ.get("OUT_DIR","out")
 LOG_DIR = os.environ.get("LOG_DIR","log")
 MAX_JSON_MB = float(os.environ.get("MAX_JSON_MB","2.5"))
 HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT","20"))
 HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES","4"))
 HTTP_BACKOFF = float(os.environ.get("HTTP_BACKOFF","0.75"))
 USER_AGENT = os.environ.get("USER_AGENT","Mozilla/5.0 (compatible; FuturesAssistant/2.2.0)")
 ENABLE_FARSIDE = os.environ.get("ENABLE_FARSIDE","1") == "1"
 ENABLE_DVOL = os.environ.get("ENABLE_DVOL","1") == "1"
 
+BINANCE_BASE = "https://api.binance.com"
+BINANCE_FUT = "https://fapi.binance.com"
+BYBIT_BASE = "https://api.bybit.com"
+OKX_BASE = "https://www.okx.com"
+
 os.makedirs(OUT_DIR, exist_ok=True)
 os.makedirs(LOG_DIR, exist_ok=True)
 
 def _http_get(url, params=None, headers=None, retries=HTTP_RETRIES):
     h = {"User-Agent": USER_AGENT}
     if headers: h.update(headers)
     last = None
     for i in range(retries):
         try:
             r = requests.get(url, params=params, headers=h, timeout=HTTP_TIMEOUT)
             if r.status_code == 200:
                 return r
         except Exception as e:
             last = e
         time.sleep(HTTP_BACKOFF * (i+1))
     raise RuntimeError(f"GET fail {url}: {last}")
 
+def _json_save(path, data):
+    with open(path,"w",encoding="utf-8") as f:
+        json.dump(data,f,ensure_ascii=False,separators=(',',':'))
+    # sanity size
+    sz = os.path.getsize(path)/1024/1024
+    return sz
+
+def _shard_and_save(prefix, data_list, bytes_limit_mb=MAX_JSON_MB):
+    # запис списку записів у частини part001.. з повагою до обмеження
+    parts = []
+    cur, cur_sz = [], 0
+    def approx_size(records):
+        return len(json.dumps(records, separators=(',',':')).encode('utf-8'))/1024/1024
+    for rec in data_list:
+        if approx_size(cur+[rec]) > bytes_limit_mb and cur:
+            parts.append(cur); cur=[]
+        cur.append(rec)
+    if cur: parts.append(cur)
+    paths=[]
+    for i,chunk in enumerate(parts, start=1):
+        p = f"{OUT_DIR}/{prefix}_part{str(i).zfill(3)}.json"
+        _json_save(p, chunk); paths.append(p)
+    return paths
+
 def collect_spot_ohlcv():
-    # v0.3.1: BTC/ETH 1d, 30d via CoinGecko (legacy minimal)
-    # v0.4.0: top-50 USDT symbols from Binance by 24h quoteVolume; TF 15m/1h/4h/1d; sharding
-    symbols = []
-    try:
-        exinfo = _http_get(BINANCE_BASE + "/api/v3/exchangeInfo").json()
-        all_usdt = [s["symbol"] for s in exinfo["symbols"] if s.get("quoteAsset")=="USDT" and s.get("status")=="TRADING"]
-        # sort by 24h quote volume desc
-        tick24 = _http_get(BINANCE_BASE + "/api/v3/ticker/24hr").json()
-        vol_map = {t["symbol"]: float(t.get("quoteVolume",0.0)) for t in tick24}
-        symbols = sorted([s for s in all_usdt if s in vol_map], key=lambda x: vol_map[x], reverse=True)[:50]
-    except Exception as e:
-        symbols = ["BTCUSDT","ETHUSDT"]  # hard fallback
+    # v0.4.0: top-50 USDT з Binance за 24h quoteVolume; TF 15m/1h/4h/1d; шардінг
+    # fallback: BTCUSDT, ETHUSDT
+    symbols = ["BTCUSDT","ETHUSDT"]
+    try:
+        exinfo = _http_get(BINANCE_BASE + "/api/v3/exchangeInfo").json()
+        cand = [s["symbol"] for s in exinfo["symbols"] if s.get("quoteAsset")=="USDT" and s.get("status")=="TRADING"]
+        tick24 = _http_get(BINANCE_BASE + "/api/v3/ticker/24hr").json()
+        vol_map = {t["symbol"]: float(t.get("quoteVolume",0.0)) for t in tick24}
+        ranked = sorted([s for s in cand if s in vol_map], key=lambda x: vol_map[x], reverse=True)
+        if len(ranked)>=2: symbols = ranked[:50]
+    except Exception as e:
+        pass
     tf_map = {"15m":"15m", "1h":"1h", "4h":"4h", "1d":"1d"}
     out_records = []
     now = int(time.time()*1000)
     for sym in symbols:
         for tf in tf_map.values():
             try:
                 kl = _http_get(BINANCE_BASE + "/api/v3/klines", params={"symbol":sym,"interval":tf,"limit":200}).json()
                 rec = {"symbol": sym, "tf": tf, "rows": [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in kl]}
                 out_records.append(rec)
             except Exception as e:
                 out_records.append({"symbol":sym,"tf":tf,"error":str(e)})
-    size = _json_save(f"{OUT_DIR}/spot_ohlcv_v2.json", out_records)
-    return {"file":"spot_ohlcv_v2.json","mb":size}
+    # шардінг
+    paths = _shard_and_save("spot_ohlcv_v2", out_records, bytes_limit_mb=MAX_JSON_MB)
+    return {"files":[os.path.basename(p) for p in paths]}
 
 def collect_derivs_signals():
-    # v0.3.1: funding+basis from Binance
+    # v0.4.0: funding now + 24h hist (Binance), OI now (Bybit/OKX) для BTCUSDT, ETHUSDT
     out = {"source":["binance"], "flags":[]}
     try:
         # funding now
         f_now = _http_get(BINANCE_FUT + "/fapi/v1/premiumIndex").json()
         if isinstance(f_now, dict): f_now=[f_now]
         out["funding_now"] = [{"symbol":x.get("symbol"), "fundingRate": float(x.get("lastFundingRate",0.0)), "markPrice": float(x.get("markPrice",0.0))} for x in f_now if x.get("symbol") in ["BTCUSDT","ETHUSDT"]]
         # funding hist 24h
         hist = {}
         for sym in ["BTCUSDT","ETHUSDT"]:
             h = _http_get(BINANCE_FUT + "/fapi/v1/fundingRate", params={"symbol":sym,"limit":200}).json()
             hist[sym] = [{"fundingRate": float(i["fundingRate"]), "fundingTime": int(i["fundingTime"])} for i in h if "fundingRate" in i]
         out["funding_hist_24h"] = hist
     except Exception as e:
         out["flags"].append(f"funding_error:{str(e)[:120]}")
+    # OI Bybit
+    try:
+        bb = _http_get(BYBIT_BASE + "/v5/market/open-interest", params={"category":"linear","symbol":"BTCUSDT","intervalTime":"5min"}).json()
+        be = _http_get(BYBIT_BASE + "/v5/market/open-interest", params={"category":"linear","symbol":"ETHUSDT","intervalTime":"5min"}).json()
+        out["bybit_oi"] = {"BTCUSDT": bb, "ETHUSDT": be}
+        out["source"].append("bybit")
+    except Exception as e:
+        out.setdefault("flags",[]).append(f"bybit_oi_error:{str(e)[:120]}")
+    # OI OKX
+    try:
+        ob = _http_get(OKX_BASE + "/api/v5/public/open-interest", params={"instType":"SWAP","uly":"BTC-USDT"}).json()
+        oe = _http_get(OKX_BASE + "/api/v5/public/open-interest", params={"instType":"SWAP","uly":"ETH-USDT"}).json()
+        out["okx_oi"] = {"BTCUSDT": ob, "ETHUSDT": oe}
+        out["source"].append("okx")
+    except Exception as e:
+        out.setdefault("flags",[]).append(f"okx_oi_error:{str(e)[:120]}")
-    size = _json_save(f"{OUT_DIR}/derivs_signals_v2.json", out)
-    return {"file":"derivs_signals_v2.json","mb":size}
+    _json_save(f"{OUT_DIR}/derivs_signals_v2.json", out)
+    return {"file":"derivs_signals_v2.json"}
 
 def collect_options_vola():
-    # v0.3.1: HV surrogate 30d for BTC
+    # v0.4.0: спроба DVOL Deribit (BTC/ETH), fallback Volmex, далі HV surrogate з штрафами
     out = {"ok": False, "source": None, "flags": []}
     try:
-        # keep minimal HV surrogate
-        out["ok"] = True
-        out["source"] = "hv_surrogate"
-        out["btc_hv30"] = 0.0
-        out["flags"].append("surrogate:hv")
+        if ENABLE_DVOL:
+            # Deribit публічний DVOL (узагальнено)
+            # Якщо недоступно - кидає виняток, підемо у фолбеки
+            # Тут залишаємо як псевдо-ендпойнт-приклад: DVOL значення заповнюємо, якщо респонс валідний
+            raise RuntimeError("deribit_dvol_placeholder")  # замінити реальною інтеграцією
+        raise RuntimeError("dvol_disabled")
     except Exception:
-        pass
-    size = _json_save(f"{OUT_DIR}/options_vola_v2.json", out)
-    return {"file":"options_vola_v2.json","mb":size}
+        # fallback Volmex (псевдо): якщо теж недоступно - HV
+        try:
+            raise RuntimeError("volmex_placeholder")
+        except Exception:
+            out["ok"] = True
+            out["source"] = "hv_surrogate"
+            out["flags"].append("surrogate:hv")
+            out["conf_penalty"] = -0.10
+    _json_save(f"{OUT_DIR}/options_vola_v2.json", out)
+    return {"file":"options_vola_v2.json"}
 
 def collect_macro_flows():
-    # v0.3.1: stablecoins total (DeFiLlama→CoinGecko), ETF rows (Farside→SoSoValue DOM)
+    # v0.4.0: покращений парсинг ETF Farside + DOM fallback SoSoValue, stablecoins DeFiLlama→CoinGecko
     out = {"stables":{}, "etf":{}, "flags":[]}
     # stablecoins (як було)
     try:
         dd = _http_get("https://stablecoins.llama.fi/stablecoins").json()
         total = float(dd.get("totalCirculatingUSD",0.0))
         out["stables"] = {"total": total, "source":"defillama"}
     except Exception as e:
         out["stables"] = {"total": None, "source":"coingecko_fallback"}
         out["flags"].append("stables:fallback:coingecko")
     # ETF Farside
     etf_rows = 0
     try:
-        if ENABLE_FARSIDE:
-            r = _http_get("https://farside.co.uk/bitcoin/")
-            tables = pd.read_html(r.text)
-            if tables:
-                df = tables[0]
-                etf_rows = len(df)
-                out["etf"] = {"rows": etf_rows, "source":"farside"}
-            else:
-                raise RuntimeError("no tables")
+        if ENABLE_FARSIDE:
+            r = _http_get("https://farside.co.uk/bitcoin/")
+            dfs = pd.read_html(r.text)
+            if dfs:
+                df = dfs[0]
+                etf_rows = int(df.shape[0])
+                out["etf"] = {"rows": etf_rows, "source":"farside"}
+            else:
+                raise RuntimeError("farside_no_tables")
         else:
             raise RuntimeError("farside_disabled")
     except Exception as e:
         # fallback SoSoValue DOM
         try:
             rr = _http_get("https://sosovalue.com/assets/etf/us-btc-spot?period=all", headers={"Referer":"https://sosovalue.com/"})
             soup = BeautifulSoup(rr.text, "lxml")
             # спрощений селектор рядків
             rows = soup.find_all("tr")
             etf_rows = max(0, len(rows)-1)
             out["etf"] = {"rows": etf_rows, "source":"sosovalue", "flags":["fallback:sosovalue"]}
         except Exception as e2:
             out["etf"] = {"rows": 0, "source":"null", "flags":["etf_error", str(e)[:80], str(e2)[:80]]}
-    size = _json_save(f"{OUT_DIR}/macro_flows_v2.json", out)
-    return {"file":"macro_flows_v2.json","mb":size}
+    _json_save(f"{OUT_DIR}/macro_flows_v2.json", out)
+    return {"file":"macro_flows_v2.json"}
 
 def build_tripack_meta(index_items):
-    # v0.3.1: conf, quorum, flags minimal
-    meta = {"conf":1.00,"log":{"quorum":"ok","missing":[],"flags":[]}}
+    # v0.4.0: policy_flags + конф-штрафи за правилами 2.1.9
+    meta = {"conf":1.00,"log":{"quorum":"ok","missing":[],"flags":[]}, "policy_flags":{}}
     # sanity BTC>1000 з spot
-    btc_ok = True
+    btc_ok = None
     try:
-        spot = json.load(open(f"{OUT_DIR}/spot_ohlcv_v2.json","r",encoding="utf-8"))
-        btc = [r for r in spot if r.get("symbol")=="BTCUSDT" and r.get("tf")=="1d"]
-        if btc and btc[0].get("rows"):
-            last = btc[0]["rows"][-1][4]
-            btc_ok = float(last) > 1000
+        # з шардінгу читаємо першу частину
+        shards = sorted([p for p in os.listdir(OUT_DIR) if p.startswith("spot_ohlcv_v2_part")])
+        if shards:
+            spot = json.load(open(f"{OUT_DIR}/{shards[0]}","r",encoding="utf-8"))
+            btc = [r for r in spot if r.get("symbol")=="BTCUSDT" and r.get("tf") in ("1d","4h")]
+            if btc and btc[0].get("rows"):
+                last = btc[0]["rows"][-1][4]
+                btc_ok = float(last) > 1000
     except Exception as e:
-        btc_ok = False
+        btc_ok = False
     if not btc_ok:
         meta["log"]["flags"].append("sanity:btc_close_le_1000_or_missing")
         meta["conf"] -= 0.02
-        meta["log"]["quorum"]="ok_fallback"
+        meta["log"]["quorum"]="ok_fallback"
+    # macro flows
+    try:
+        mf = json.load(open(f"{OUT_DIR}/macro_flows_v2.json","r",encoding="utf-8"))
+        st_total = mf.get("stables",{}).get("total")
+        etf_rows = mf.get("etf",{}).get("rows",0)
+        etf_source = mf.get("etf",{}).get("source")
+        if etf_source=="sosovalue":
+            meta["conf"] -= 0.05
+            meta["log"]["flags"].append("etf:fallback:sosovalue")
+        if st_total is None or st_total <= 1e9 or etf_rows <= 0:
+            meta["log"]["quorum"]="ok_fallback"
+            meta["log"]["flags"].append("macro:weak_or_missing")
+        if st_total is not None and st_total < 220e9:
+            meta["log"]["flags"].append("stables_suspect_lt_220B")
+    except Exception as e:
+        meta["log"]["quorum"]="ok_fallback"
+        meta["log"]["flags"].append("macro:missing")
+        meta["conf"] -= 0.01
+    # options vola penalties
+    try:
+        ov = json.load(open(f"{OUT_DIR}/options_vola_v2.json","r",encoding="utf-8"))
+        if ov.get("source")=="hv_surrogate":
+            meta["conf"] -= 0.10
+            meta["log"]["flags"].append("vol:surrogate:hv")
+    except Exception:
+        meta["log"]["flags"].append("vol:missing")
+        meta["conf"] -= 0.01
+    # observability: echo/dir glitch mitigation flag (застосуємо -0.01, поки не буде health.json)
+    meta["conf"] = round(max(0.0, meta["conf"] - 0.01), 4)
+    meta["log"]["flags"].append("observability:cloud_dir_render_error")
+    # policy flags (Freeze/Leader-lock/Flow-alignment/mHedge) - спрощений проксі
+    pf = {}
+    # Freeze: тут потребує ΔTS_BTC (нема) - пропускаємо
+    # Leader-lock: без ETF z-score - оцінюємо проксі за наявністю etf_rows
+    pf["leader-lock"] = "OFF" if etf_rows and etf_rows>0 else "ON"
+    # Flow-alignment: без Δ7d(stables) - UNC
+    pf["flow-alignment"] = "UNC"
+    pf["mHedge"] = "UNC"
+    meta["policy_flags"] = pf
     return meta
 
 def main():
     started = time.time()
-    idx = []
+    idx = {"files":{}}
     # 1) Spot
-    res_spot = collect_spot_ohlcv(); idx.append(res_spot)
+    res_spot = collect_spot_ohlcv(); idx["files"]["spot"] = res_spot
     # 2) Derivs
-    res_der = collect_derivs_signals(); idx.append(res_der)
+    res_der = collect_derivs_signals(); idx["files"]["derivs"] = res_der
     # 3) Options vola
-    res_vol = collect_options_vola(); idx.append(res_vol)
+    res_vol = collect_options_vola(); idx["files"]["vola"] = res_vol
     # 4) Macro flows
-    res_mac = collect_macro_flows(); idx.append(res_mac)
+    res_mac = collect_macro_flows(); idx["files"]["macro"] = res_mac
     # 5) Tripack meta
-    meta = build_tripack_meta(idx)
-    _json_save(f"{OUT_DIR}/tripack_meta_v2.json", meta)
-    _json_save(f"{OUT_DIR}/index.json", {"generated_at": datetime.utcnow().isoformat()+"Z", "items": idx, "conf": meta["conf"]})
+    meta = build_tripack_meta(idx)
+    _json_save(f"{OUT_DIR}/tripack_meta_v2.json", meta)
+    _json_save(f"{OUT_DIR}/index.json", {"generated_at": datetime.utcnow().isoformat()+"Z", "items": idx, "conf": meta["conf"], "policy_flags": meta.get("policy_flags",{})})
     print("ok")
 
 if __name__=="__main__":
     main()
diff --git a/CHANGELOG.md b/CHANGELOG.md
index 4b7a1f0..9a3e3e2 100644
--- a/CHANGELOG.md
+++ b/CHANGELOG.md
@@ -5,6 +5,20 @@ and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0
 ## [Unreleased]
 ### Added
 - GitHub Actions (planned): lint, mypy, JSON-schema checks, smoke-тести фолбеків.
 - Observability (planned): OpenTelemetry traceId/spanId у логах.
 - Soft-corroboration (planned): перехресна перевірка ETF-метрик.
 
+## [2.2.0] - 2025-10-28
+### Added
+- Spot: top-50 USDT by 24h quoteVolume з Binance, TF 15m/1h/4h/1d, шардінг `spot_ohlcv_v2_partNNN.json`.
+- Derivs: funding now + 24h hist (Binance), OI now з Bybit/OKX.
+- ETF parser: Farside read_html + SoSoValue DOM fallback із `flags` і штрафом conf.
+- Policy flags у `tripack_meta_v2.json` та підсумковий `index.json`.
+- Echo health у `logs_cloud` для швидкого аудиту кворуму без відкриття JSON.
+
+### Changed
+- `tripack_meta_v2` - нарахування conf згідно RULES 2.1.9 та службовий прапорець `observability:cloud_dir_render_error`.
+
+### Fixed
+- Стабільність push артефактів і читабельність при збоях UI GitHub.
+
diff --git a/PROMPT_ALL_IN_ONE.md b/PROMPT_ALL_IN_ONE.md
index 2baf1b1..56fd0e1 100644
--- a/PROMPT_ALL_IN_ONE.md
+++ b/PROMPT_ALL_IN_ONE.md
@@ -24,6 +24,7 @@ ETF rows=<N> z1d=<..> z5d=<..> source=<farside|sosovalue>
 ## Кворум/Політики
 `tripack_meta_v2.log.quorum=<ok|ok_fallback|fail>`; `missing=[...]`; `policy_flags={...}`
+> Показуй `policy_flags` як [leader-lock, flow-alignment, mHedge] для прозорості у висновках.
 
 > Усі числа — лише з 5 JSON. Якщо блок порожній, відображаємо `source/flags` і штрафи.
