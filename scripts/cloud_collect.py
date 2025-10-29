diff --git a/scripts/cloud_collect.py b/scripts/cloud_collect.py
index 7f90a10..c3b61a2 100644
--- a/scripts/cloud_collect.py
+++ b/scripts/cloud_collect.py
@@ -217,7 +217,34 @@ def main():
     print("ok")
 
 if __name__=="__main__":
-    main()
+    try:
+        main()
+    except Exception as e:
+        # аварійний health та мінімальний index, щоб артефакти завжди були
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
+        try:
+            _json_save(f"{OUT_DIR}/tripack_meta_v2.json", fail_meta)
+            _json_save(f"{OUT_DIR}/index.json", {
+                "generated_at": datetime.utcnow().isoformat()+"Z",
+                "items": {"files":{}},
+                "conf": 0.0,
+                "policy_flags": {}
+            })
+            # окремий простий health
+            _json_save(f"{OUT_DIR}/health.json", {"ok": False, "error": str(e)[:500]})
+        finally:
+            raise
