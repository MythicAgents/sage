#!/usr/bin/env python3
"""Prove the FULL ingest mechanism end-to-end: file_upload the staged essos zip into BloodHound CE
and confirm ESSOS.LOCAL populates. Run via the MCP env:
  uv --directory /home/john/dev/bloodhound_mcp run python /home/john/dev/sage/skills/sage-trace-analysis/scripts/ingest_verify.py
"""
import os, sys, json, time
from pathlib import Path
sys.path.insert(0, "/home/john/dev/bloodhound_mcp")
from lib.bloodhound_api import BloodhoundBaseClient, FileUploadClient  # noqa: E402

STAGED = "/tmp/sage_file_staging/20260605111337_sysreport_castelblack_essos_2.zip"
OUT_DIR = Path(os.environ.get("SAGE_TRACE_OUTPUT_DIR", "/tmp/sage-trace-analysis"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT = str(OUT_DIR / "ingest_verify.out")
lines = []
def w(s=""): lines.append(str(s)); print(s, flush=True)

c = BloodhoundBaseClient()

def domains():
    r = c._request("GET", "/api/v2/available-domains")
    try:
        return [(x.get("name"), x.get("collected")) for x in r.json().get("data", [])]
    except Exception:
        return [("<parse-err>", r.text[:120])]

w("=== available-domains BEFORE ingest ===")
for n in domains():
    w(f"  {n}")

w(f"\n=== file_upload(upload_collection_file) staged essos zip ===\n  {STAGED}")
fu = FileUploadClient(c)
try:
    res = fu.upload_collection_file(STAGED)
    w(f"  upload result: {json.dumps(res, default=str)}")
except Exception as e:
    w(f"  UPLOAD ERROR: {type(e).__name__}: {e}")

# Poll for ingest to process (async on the server side)
w("\n=== polling available-domains for ESSOS population (up to ~90s) ===")
essos_seen = False
for i in range(18):
    time.sleep(5)
    ds = domains()
    essos = [d for d in ds if d[0] and "ESSOS" in str(d[0]).upper()]
    w(f"  t+{(i+1)*5}s: domains={[d[0] for d in ds]}")
    if essos:
        essos_seen = True
        w(f"  --> ESSOS present: {essos}")
        break

w(f"\nRESULT: essos_seen={essos_seen}")
with open(OUT, "w") as f:
    f.write("\n".join(lines))
