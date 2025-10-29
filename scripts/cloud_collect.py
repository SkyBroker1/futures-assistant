@@
 if __name__=="__main__":
-    try:
-        main()
-    except Exception as e:
-        # аварійний health та мінімальний index, щоб артефакти завжди були
-        try:
-            os.makedirs(OUT_DIR, exist_ok=True)
-        except Exception:
-            pass
-        fail_meta = {
-            "conf": 0.0,
-            "log": {
-                "quorum": "fail",
-                "missing": ["spot_ohlcv_v2*", "derivs_signals_v2.json", "options_vola_v2.json", "macro_flows_v2.json"],
-                "flags": [f"collector:exception:{str(e)[:200]}"]
-            },
-            "policy_flags": {}
-        }
-        try:
-            _json_save(f"{OUT_DIR}/tripack_meta_v2.json", fail_meta)
-            _json_save(f"{OUT_DIR}/index.json", {
-                "generated_at": datetime.utcnow().isoformat()+"Z",
-                "items": {"files":{}},
-                "conf": 0.0,
-                "policy_flags": {}
-            })
-            # окремий простий health
-            _json_save(f"{OUT_DIR}/health.json", {"ok": False, "error": str(e)[:500]})
-        finally:
-            raise
+    try:
+        main()
+    except Exception as e:
+        # Аварійний шлях: створюємо мінімальні файли й завершуємося УСПІШНО,
+        # щоб workflow продовжив кроки upload/publish і ми мали що проаудитити.
+        try:
+            os.makedirs(OUT_DIR, exist_ok=True)
+        except Exception:
+            pass
+        fail_meta = {
+            "conf": 0.0,
+            "log": {
+                "quorum": "fail",
+                "missing": ["spot_ohlcv_v2*", "derivs_signals_v2.json", "options_vola_v2.json", "macro_flows_v2.json"],
+                "flags": [f"collector:exception:{str(e)[:200]}"]
+            },
+            "policy_flags": {}
+        }
+        _json_save(f"{OUT_DIR}/tripack_meta_v2.json", fail_meta)
+        _json_save(f"{OUT_DIR}/index.json", {
+            "generated_at": datetime.utcnow().isoformat()+"Z",
+            "items": {"files":{}},
+            "conf": 0.0,
+            "policy_flags": {}
+        })
+        _json_save(f"{OUT_DIR}/health.json", {"ok": False, "error": str(e)[:500]})
+        print(f"[collector-fail] {e}", flush=True)
+        # НЕ підіймаємо далі виняток: даємо workflow завершити upload/publish
+        # і відмітимо проблему у conf/flags.
