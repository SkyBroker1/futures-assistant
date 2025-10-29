diff --git a/scripts/cloud_collect.py b/scripts/cloud_collect.py
index c3b61a2..e9a5d77 100644
--- a/scripts/cloud_collect.py
+++ b/scripts/cloud_collect.py
@@
 BINANCE_BASE = "https://api.binance.com"
 BINANCE_FUT = "https://fapi.binance.com"
 BYBIT_BASE = "https://api.bybit.com"
 OKX_BASE = "https://www.okx.com"
+COINGECKO_BASE = "https://api.coingecko.com/api/v3"
 
@@
 def collect_spot_ohlcv():
@@
-            except Exception as e:
-                _log_err(f"spot:klines {sym} {tf} {e}")
-                out_records.append({"symbol": sym, "tf": tf, "error": str(e)[:200]})
+            except Exception as e:
+                # Fallback 1: OKX candles
+                try:
+                    inst = sym.replace("USDT","-USDT")
+                    # OKX returns most-recent first: [ts, o,h,l,c, vol, volCcy, volCcyQuote]
+                    resp = _http_get(OKX_BASE + "/api/v5/market/candles", params={"instId": inst, "bar": tf}).json()
+                    data = resp.get("data", [])
+                    rows = []
+                    for r in reversed(data[-200:]):
+                        ts, o, h, l, c, vol = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
+                        rows.append([ts, o, h, l, c, vol])
+                    if rows:
+                        out_records.append({"symbol": sym, "tf": tf, "rows": rows, "source":"okx"})
+                        continue
+                    raise RuntimeError("okx_empty")
+                except Exception as e2:
+                    _log_err(f"spot:klines_fallback {sym} {tf} okx {e2}")
+                    # Fallback 2: CoinGecko (daily only)
+                    try:
+                        if tf == "1d":
+                            cg_id = "bitcoin" if sym=="BTCUSDT" else ("ethereum" if sym=="ETHUSDT" else None)
+                            if cg_id:
+                                r = _http_get(COINGECKO_BASE + f"/coins/{cg_id}/market_chart", params={"vs_currency":"usd","days":"200"}).json()
+                                rows=[]
+                                for p, price in r.get("prices", []):
+                                    # ціна лише close, наближаємо OHLC до close
+                                    c = float(price); ts=int(p)
+                                    rows.append([ts, c, c, c, c, 0.0])
+                                if rows:
+                                    out_records.append({"symbol": sym, "tf": tf, "rows": rows, "source":"coingecko"})
+                                    continue
+                    except Exception as e3:
+                        _log_err(f"spot:klines_fallback {sym} {tf} coingecko {e3}")
+                    # якщо всі фолбеки впали - фіксуємо помилку
+                    out_records.append({"symbol": sym, "tf": tf, "error": str(e)[:200]})
@@
 def collect_derivs_signals():
@@
-    # funding now + 24h hist (Binance)
+    # funding now + 24h hist (Binance) → fallbacks OKX
     try:
@@
     except Exception as e:
         _log_err(f"derivs:binance {e}")
         out["flags"].append(f"funding_error:{str(e)[:120]}")
+        # OKX funding history fallback
+        try:
+            def okx_funding_hist(uly):
+                j = _http_get(OKX_BASE + "/api/v5/public/funding-rate-history", params={"instId": f"{uly}-SWAP","limit":"200"}).json()
+                arr = j.get("data",[])
+                return [{"fundingRate": float(x[1]), "fundingTime": int(x[0])} for x in arr]
+            out["funding_hist_24h"] = {
+                "BTCUSDT": okx_funding_hist("BTC-USDT"),
+                "ETHUSDT": okx_funding_hist("ETH-USDT"),
+            }
+            out["source"].append("okx_funding")
+        except Exception as e2:
+            _log_err(f"derivs:okx_funding {e2}")
+            out["flags"].append(f"okx_funding_error:{str(e2)[:120]}")
@@
 def collect_macro_flows():
@@
-    try:
-        dd = _http_get("https://stablecoins.llama.fi/stablecoins").json()
-        total = float(dd.get("totalCirculatingUSD", 0.0))
-        out["stables"] = {"total": total, "source": "defillama"}
-    except Exception as e:
+    try:
+        dd = _http_get("https://stablecoins.llama.fi/stablecoins").json()
+        total = float(dd.get("totalCirculatingUSD", 0.0))
+        if total <= 0.0:
+            raise RuntimeError("defillama_zero")
+        out["stables"] = {"total": total, "source": "defillama"}
+    except Exception as e:
         _log_err(f"macro:defillama {e}")
-        out["stables"] = {"total": None, "source": "coingecko_fallback"}
-        out["flags"].append("stables:fallback:coingecko")
+        # CoinGecko fallback: сумуємо капи основних стейблів
+        try:
+            ids = ",".join(["tether","usd-coin","dai","first-digital-usd","usdd","true-usd","paxos-standard","frax","usde"])
+            r = _http_get(COINGECKO_BASE + "/coins/markets", params={"vs_currency":"usd","ids":ids,"per_page":"250","page":"1"}).json()
+            total = sum(float(x.get("market_cap") or 0.0) for x in r)
+            out["stables"] = {"total": total, "source": "coingecko"}
+            out["flags"].append("stables:fallback:coingecko")
+        except Exception as e2:
+            _log_err(f"macro:coingecko {e2}")
+            out["stables"] = {"total": None, "source": "null"}
+            out["flags"].append("stables:error")
@@
-        if ENABLE_FARSIDE:
-            r = _http_get("https://farside.co.uk/bitcoin/")
+        if ENABLE_FARSIDE:
+            r = _http_get("https://farside.co.uk/bitcoin/", headers={"User-Agent": USER_AGENT, "Accept":"text/html"})
             dfs = pd.read_html(r.text)
