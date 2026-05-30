"""One-time data seeding to Render over HTTPS.

SSH/scp to Render is blocked on Accora's network (port 22 intercepted),
so we push the local out/ data to the /data disk via the token-guarded
/api/admin/upload endpoint instead.

Usage:
    set IFU_UPLOAD_TOKEN=<token>
    python upload_data_https.py

Re-running is safe (idempotent overwrite). After it completes, the token
env var should be cleared on Render to disable the endpoint.
"""
import os
import sys
from pathlib import Path

# Accora's network does SSL inspection (MITM proxy).  Python's bundled
# certifi store doesn't include the corporate root CA, but the Windows
# certificate store does (it's why curl works).  truststore makes Python's
# ssl use the OS store, so TLS verification stays ON and trusts the
# corporate CA -- no need to disable verification.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

BASE = os.environ.get(
    "IFU_REMOTE_BASE", "https://ifu-api-xmm5.onrender.com").rstrip("/")
TOKEN = os.environ.get("IFU_UPLOAD_TOKEN", "")
OUT = Path(__file__).resolve().parent / "out"
FOLDERS = ("figures", "views", "projects", "sources", "imports")

if not TOKEN:
    print("ERROR: set IFU_UPLOAD_TOKEN in the environment first.")
    sys.exit(1)

session = requests.Session()
session.headers["X-Upload-Token"] = TOKEN
# TLS verification stays ON; truststore (above) makes it trust the
# corporate CA via the Windows store.

total_files = 0
total_bytes = 0
failures = []

for folder in FOLDERS:
    root = OUT / folder
    if not root.is_dir():
        print(f"  skip {folder}/ (not found locally)")
        continue
    files = [p for p in root.rglob("*") if p.is_file()]
    print(f"\n=== {folder}/  ({len(files)} files) ===")
    for p in files:
        rel = p.relative_to(OUT).as_posix()
        data = p.read_bytes()
        try:
            r = session.post(
                f"{BASE}/api/admin/upload",
                headers={"X-Rel-Path": rel,
                         "Content-Type": "application/octet-stream"},
                data=data,
                timeout=120,
            )
            if r.status_code == 200:
                total_files += 1
                total_bytes += len(data)
                print(f"  ok  {rel}  ({len(data):,} B)")
            else:
                failures.append((rel, r.status_code, r.text[:120]))
                print(f"  ERR {rel}  -> {r.status_code} {r.text[:120]}")
        except Exception as e:
            failures.append((rel, "exc", str(e)[:120]))
            print(f"  ERR {rel}  -> {e}")

print(f"\n=== uploaded {total_files} files, {total_bytes:,} bytes ===")
if failures:
    print(f"!! {len(failures)} failures:")
    for rel, code, msg in failures[:20]:
        print(f"   {rel}  {code}  {msg}")
    sys.exit(2)
print("All good. Now clear IFU_UPLOAD_TOKEN on Render to disable the endpoint.")
