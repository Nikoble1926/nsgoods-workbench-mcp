#!/usr/bin/env python3
"""Rebuild /root/workbench-mcp/index.sqlite from the full scans.jsonl (idempotent).
Read-only over scans.jsonl; writes a fresh sqlite (temp then atomic replace)."""
import json, sqlite3, os, time
from urllib.parse import urlparse

# Path overrides (env-var, default = production paths so the rebuild unit needs NO env).
DATA_DIR = os.environ.get("WORKBENCH_DATA_DIR", "/root/payability-observatory/data")
SCANS = os.environ.get("WORKBENCH_SCANS", os.path.join(DATA_DIR, "scans.jsonl"))
OUT   = os.environ.get("WORKBENCH_DB", "/root/workbench-mcp/index.sqlite")
TMP   = OUT + ".tmp"

def host_of(res): 
    try: return urlparse(res).netloc
    except Exception: return ""

def main():
    t0 = time.time()
    if os.path.exists(TMP): os.remove(TMP)
    db = sqlite3.connect(TMP)
    db.executescript("""
    CREATE TABLE verdicts(scan_id TEXT, scanned_at TEXT, resource TEXT, host TEXT,
        verdict TEXT, payable_networks TEXT, options TEXT);
    CREATE TABLE resources(resource TEXT PRIMARY KEY, host TEXT, first_seen TEXT, last_seen TEXT,
        last_scan_id TEXT, last_verdict TEXT, changes INTEGER);
    CREATE TABLE hosts(host TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT,
        n_resources INTEGER, n_payable_last INTEGER, last_verdict_mix TEXT, gone_since TEXT);
    CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE prices(resource TEXT PRIMARY KEY, scheme TEXT, network TEXT, asset TEXT,
        amount_raw TEXT, pay_to TEXT, max_timeout INTEGER, n_accepts INTEGER, observed_at TEXT, source TEXT);
    CREATE TABLE price_history(resource TEXT, observed_at TEXT, network TEXT, asset TEXT,
        amount_raw TEXT, source TEXT);
    """)
    res_state = {}   # resource -> dict(host,first_seen,last_seen,last_scan_id,last_verdict,changes,prev_verdict)
    scan_time = {}   # scan_id -> scanned_at (max)
    n=0
    vbuf=[]
    def _s(v):
        return v if (v is None or isinstance(v,str)) else json.dumps(v)
    def _first_accept(items):
        first=None
        for a in items:
            if a.get("amount") is not None: first=a; break
        if first is None and items: first=items[0]
        return first
    seen_hist=set(); nhist=0
    price_best={}   # resource -> (observed_at, prices-insert-tuple), greatest observed_at wins
    for line in open(SCANS, errors="replace"):
        line=line.strip()
        if not line: continue
        try: d=json.loads(line)
        except Exception: continue
        r=d.get("resource",""); 
        if not r: continue
        sid=d.get("scan_id"); at=d.get("scanned_at",""); v=d.get("verdict")
        if v is None or v=="": v="UNKNOWN"
        h=host_of(r)
        pn=json.dumps(d.get("payable_networks") or [])
        opt=json.dumps(d.get("options") or [])
        vbuf.append((sid,at,r,h,v,pn,opt)); n+=1
        # scan price ingest: records whose options carry an amount (>=2026-09-17 in practice)
        opts_list=d.get("options") or []
        if any((o.get("amount") not in (None,"")) for o in opts_list):
            for o in opts_list:
                k=(r, at, _s(o.get("network")), _s(o.get("asset")))
                if k in seen_hist: continue
                seen_hist.add(k)
                db.execute("INSERT INTO price_history VALUES(?,?,?,?,?,?)",
                    (r, at, _s(o.get("network")), _s(o.get("asset")), _s(o.get("amount")), "scan"))
                nhist+=1
            fa=_first_accept(opts_list)
            if fa is not None:
                cur_at=price_best.get(r,(None,))[0]
                if cur_at is None or (at or "")>cur_at:
                    price_best[r]=(at, (r, _s(fa.get("scheme")), _s(fa.get("network")), _s(fa.get("asset")),
                                        _s(fa.get("amount")), _s(fa.get("pay_to")), fa.get("max_timeout_seconds"),
                                        len(opts_list), at, "scan"))
        if len(vbuf)>=5000:
            db.executemany("INSERT INTO verdicts VALUES(?,?,?,?,?,?,?)", vbuf); vbuf=[]
        if sid and at: scan_time[sid]=max(scan_time.get(sid,""), at)
        st=res_state.get(r)
        if st is None:
            res_state[r]={"host":h,"first_seen":at,"last_seen":at,"last_scan_id":sid,
                          "last_verdict":v,"changes":0,"prev":v}
        else:
            if at>st["last_seen"]:
                st["last_seen"]=at; st["last_scan_id"]=sid
                if v!=st["prev"]: st["changes"]+=1
                st["prev"]=v; st["last_verdict"]=v
            if at<st["first_seen"]: st["first_seen"]=at
    if vbuf: db.executemany("INSERT INTO verdicts VALUES(?,?,?,?,?,?,?)", vbuf)
    # resources
    db.executemany("INSERT INTO resources VALUES(?,?,?,?,?,?,?)",
        [(r,s["host"],s["first_seen"],s["last_seen"],s["last_scan_id"],s["last_verdict"],s["changes"])
         for r,s in res_state.items()])
    # last FULL scan = the scan_id with the most rows
    # last FULL scan = latest scanned_at among non-sample scans (>2000 rows); samples are ~500
    cur=db.execute("SELECT scan_id FROM verdicts GROUP BY scan_id HAVING COUNT(*)>2000 ORDER BY MAX(scanned_at) DESC LIMIT 1")
    row=cur.fetchone()
    last_full=row[0] if row else db.execute("SELECT scan_id FROM verdicts GROUP BY scan_id ORDER BY MAX(scanned_at) DESC LIMIT 1").fetchone()[0]
    # hosts aggregate (based on last_full presence)
    hosts={}
    for r,s in res_state.items():
        h=s["host"]; hs=hosts.setdefault(h,{"first":s["first_seen"],"last":s["last_seen"],"n":0})
        hs["n"]+=1; hs["first"]=min(hs["first"],s["first_seen"]); hs["last"]=max(hs["last"],s["last_seen"])
    # n_payable_last + verdict mix from last_full
    mix={}; payl={}
    for row in db.execute("SELECT host,verdict FROM verdicts WHERE scan_id=?",(last_full,)):
        h,v=row; mix.setdefault(h,{}); mix[h][v]=mix[h].get(v,0)+1
        if v=="PAYABLE": payl[h]=payl.get(h,0)+1
    db.executemany("INSERT INTO hosts VALUES(?,?,?,?,?,?,?)",
        [(h,hs["first"],hs["last"],hs["n"],payl.get(h,0),json.dumps(mix.get(h,{})),
          (None if h in mix else hs["last"][:10])) for h,hs in hosts.items()])
    db.execute("INSERT INTO meta VALUES('last_full_scan_id',?)",(last_full,))
    db.execute("INSERT INTO meta VALUES('last_full_scanned_at',?)",(scan_time.get(last_full,""),))
    db.execute("INSERT INTO meta VALUES('n_verdict_rows',?)",(str(n),))
    db.execute("INSERT INTO meta VALUES('built_at',?)",(scan_time.get(last_full,""),))  # deterministic
    db.execute("CREATE INDEX ix_v_res ON verdicts(resource)")
    db.execute("CREATE INDEX ix_v_host ON verdicts(host)")
    # ---- prices + price_history from ALL prices-*.jsonl sweeps (oldest->newest) ----
    # NOTE: scan-sourced price_history rows were already ingested in the scans loop above,
    # sharing the same seen_hist dedup set and price_best (greatest observed_at wins).
    import glob as _glob
    pfiles=sorted(_glob.glob(os.path.join(DATA_DIR, "prices-*.jsonl")))
    for pf in pfiles:
        for line in open(pf, errors="replace"):
            line=line.strip()
            if not line: continue
            try: d=json.loads(line)
            except Exception: continue
            acc=d.get("accepts") or []
            res=d.get("resource"); oat=d.get("observed_at")
            # price_history: every accept, deduped on (resource, observed_at, network, asset)
            for a in acc:
                key=(res, oat, _s(a.get("network")), _s(a.get("asset")))
                if key in seen_hist: continue
                seen_hist.add(key)
                db.execute("INSERT INTO price_history VALUES(?,?,?,?,?,?)",
                    (res, oat, _s(a.get("network")), _s(a.get("asset")), _s(a.get("amount")), "sweep"))
                nhist+=1
            # prices candidate: FIRST accept with an amount, else first accept; keep greatest observed_at
            fa=_first_accept(acc)
            if fa is not None:
                cur_at=price_best.get(res,(None,))[0]
                if cur_at is None or (oat or "")>cur_at:
                    price_best[res]=(oat, (res, _s(fa.get("scheme")), _s(fa.get("network")), _s(fa.get("asset")),
                                          _s(fa.get("amount")), _s(fa.get("pay_to")), fa.get("max_timeout_seconds"),
                                          len(acc), oat, "sweep"))
    # write prices = the greatest-observed_at observation per resource (across scan + sweep)
    npr=0
    for res,(oat,tup) in price_best.items():
        db.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?,?,?)", tup)
        npr+=1
    ph_from=db.execute("SELECT MIN(observed_at) FROM price_history").fetchone()[0]
    ph_to=db.execute("SELECT MAX(observed_at) FROM price_history").fetchone()[0]
    db.execute("INSERT OR REPLACE INTO meta VALUES('prices_source', ?)",
               ("scan+sweep: %s + %s" % (last_full, (pfiles[-1] if pfiles else "")),))
    db.execute("INSERT OR REPLACE INTO meta VALUES('n_prices', ?)", (str(npr),))
    db.execute("INSERT OR REPLACE INTO meta VALUES('n_price_history', ?)", (str(nhist),))
    db.execute("INSERT OR REPLACE INTO meta VALUES('price_history_from', ?)", (ph_from or "",))
    db.execute("INSERT OR REPLACE INTO meta VALUES('price_history_to', ?)", (ph_to or "",))
    db.execute("CREATE INDEX ix_ph_res ON price_history(resource)")
    db.commit(); db.close()
    os.replace(TMP, OUT)
    dt=time.time()-t0
    sz=os.path.getsize(OUT)
    print(f"built {OUT}  rows={n}  resources={len(res_state)}  hosts={len(hosts)}  last_full={last_full}  {dt:.1f}s  {sz/1e6:.1f}MB")

if __name__=="__main__": main()
