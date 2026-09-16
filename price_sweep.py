#!/usr/bin/env python3
"""One-off price sweep: GET each PAYABLE resource (no payment), parse ALL accepts of the 402,
persist amount/scheme/etc to prices-YYYYMMDD.jsonl. Never writes scans.jsonl."""
import json, sqlite3, base64, threading, urllib.request, urllib.error, socket
from concurrent import futures
from datetime import datetime, timezone
from urllib.parse import urlparse
from collections import defaultdict, Counter

import os
DATA_DIR = os.environ.get("WORKBENCH_DATA_DIR", os.environ.get("WORKBENCH_DIR", "."))
DB=os.environ.get("WORKBENCH_DB", os.path.join(os.environ.get("WORKBENCH_DIR","."), "index.sqlite"))
OUT=os.environ.get("WORKBENCH_PRICES_OUT", os.path.join(DATA_DIR, "prices-sweep.jsonl"))
UA="nsgoods-observatory/price-sweep (+https://x402.nsgoods.org)"
TIMEOUT=10; TOTAL_WORKERS=20; PER_HOST=4

class Redir(urllib.request.HTTPRedirectHandler):
    max_redirections=5

def host_of(r):
    try: return urlparse(r).netloc
    except Exception: return ""

_host_sem=defaultdict(lambda: threading.Semaphore(PER_HOST))
_host_lock=threading.Lock()
def sem(h):
    with _host_lock: return _host_sem[h]

def parse_accepts(body, hdr):
    ch=None
    try:
        b=json.loads(body) if body and body.strip() else {}
        if isinstance(b,dict) and b.get("accepts"): ch=b
    except Exception: pass
    if ch is None and hdr:
        try:
            pad="="*(-len(hdr)%4); ch=json.loads(base64.b64decode(hdr+pad))
        except Exception: ch=None
    out=[]
    if ch and isinstance(ch.get("accepts"),list):
        for a in ch["accepts"]:
            out.append({"scheme":a.get("scheme"),"network":a.get("network"),"asset":a.get("asset"),
                        "amount":a.get("amount") or a.get("maxAmountRequired"),
                        "pay_to":a.get("payTo") or a.get("pay_to"),
                        "max_timeout_seconds":a.get("maxTimeoutSeconds")})
    return out

opener=urllib.request.build_opener(Redir())
def probe(resource):
    h=host_of(resource); rec={"resource":resource,"host":h,
        "observed_at":datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status":None,"accepts":[],"error":None}
    s=sem(h); s.acquire()
    try:
        req=urllib.request.Request(resource, headers={"User-Agent":UA})
        try:
            r=opener.open(req, timeout=TIMEOUT); rec["status"]=r.getcode(); r.read(1)
        except urllib.error.HTTPError as e:
            rec["status"]=e.code
            if e.code==402:
                rec["accepts"]=parse_accepts(e.read(), e.headers.get("PAYMENT-REQUIRED"))
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            rec["error"]=f"{type(e).__name__}: {str(e)[:80]}"
        except Exception as e:
            rec["error"]=f"{type(e).__name__}: {str(e)[:80]}"
    finally:
        s.release()
    return rec

def main():
    c=sqlite3.connect(DB)
    lastfull=c.execute("SELECT v FROM meta WHERE k='last_full_scan_id'").fetchone()[0]
    res=[r[0] for r in c.execute("SELECT resource FROM verdicts WHERE scan_id=? AND verdict='PAYABLE'",(lastfull,))]
    c.close()
    print(f"sweeping {len(res)} PAYABLE resources", flush=True)
    n=0; n402=0; namt=0; stat=Counter()
    with open(OUT,"w") as out, futures.ThreadPoolExecutor(max_workers=TOTAL_WORKERS) as ex:
        for rec in ex.map(probe, res):
            out.write(json.dumps(rec)+"\n"); n+=1
            stat[rec["status"] if rec["status"] is not None else "ERR"]+=1
            if rec["status"]==402: n402+=1
            if any(a.get("amount") for a in rec["accepts"]): namt+=1
            if n%1000==0: print(f"  {n}/{len(res)}  402={n402}  with_amount={namt}", flush=True)
    print("=== SWEEP DONE ===", flush=True)
    print(f"total={n}  http_402={n402}  with_amount={namt}  coverage={100*namt/n:.1f}%", flush=True)
    print("status histogram (non-402 = drift):", dict(stat), flush=True)

if __name__=="__main__": main()
