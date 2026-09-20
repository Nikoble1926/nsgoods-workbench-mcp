#!/usr/bin/env python3
"""nsgoods Workbench MCP  -  Round 2c (free layer, useful first-answer). Streamable HTTP on
127.0.0.1:4036 /mcp. Read-only over the payability index + watch state + manifest."""
import json, os, sqlite3, time, fcntl, tempfile, urllib.request
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from starlette.routing import Route
from starlette.responses import JSONResponse
from eth_account import Account
from eth_account.messages import encode_defunct

WORKBENCH_DIR = os.environ.get("WORKBENCH_DIR", ".")
DATA_DIR = os.environ.get("WORKBENCH_DATA_DIR", WORKBENCH_DIR)
DB = os.environ.get("WORKBENCH_DB", os.path.join(WORKBENCH_DIR, "index.sqlite"))
WATCH_X401 = os.environ.get("WORKBENCH_X401", os.path.join(DATA_DIR, "x401_adoption_state.json"))
WATCH_DRIFT = os.environ.get("WORKBENCH_DRIFT", os.path.join(DATA_DIR, "model_claim_state.json"))
MANIFEST = os.environ.get("WORKBENCH_MANIFEST", "https://x402.nsgoods.org/proof/index.json")
PAYABILITY_INDEX = "https://x402.nsgoods.org/ata-audit/payability_index.json"
FP_STORE = os.environ.get("WORKBENCH_FP_STORE", os.path.join(tempfile.gettempdir(), "nsgoods_fp_limit_workbench.json"))
FP_LIMIT = 300
N_TOOLS = 8

def _ensure_db():
    if os.path.exists(DB): return
    c=sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS verdicts(scan_id TEXT, scanned_at TEXT, resource TEXT, host TEXT,
        verdict TEXT, payable_networks TEXT, options TEXT);
    CREATE TABLE IF NOT EXISTS resources(resource TEXT PRIMARY KEY, host TEXT, first_seen TEXT,
        last_seen TEXT, last_scan_id TEXT, last_verdict TEXT, changes INTEGER);
    CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY, first_seen TEXT, last_seen TEXT,
        n_resources INTEGER, n_payable_last INTEGER, last_verdict_mix TEXT, gone_since TEXT);
    CREATE TABLE IF NOT EXISTS prices(resource TEXT PRIMARY KEY, scheme TEXT, network TEXT, asset TEXT,
        amount_raw TEXT, pay_to TEXT, max_timeout INTEGER, n_accepts INTEGER, observed_at TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS price_history(resource TEXT, observed_at TEXT, network TEXT, asset TEXT,
        amount_raw TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    c.commit(); c.close()

def _db():
    c = sqlite3.connect(DB, check_same_thread=False); c.row_factory = sqlite3.Row; return c

def _meta():
    c=_db(); m={k:v for k,v in c.execute("SELECT k,v FROM meta")}; c.close(); return m

def _stamp(d: dict) -> dict:
    m=_meta()
    d["as_of"]=(m.get("last_full_scanned_at") or "")[:10]
    d["index_scan_id"]=m.get("last_full_scan_id")
    d["freshness_note"]="index rebuilt after each weekly full scan; for a live check use the paid /payable endpoint"
    return d

# ---- manifest cache (5 min) ----
_mc = {"t":0,"d":None,"at":None}
def _manifest():
    if time.time()-_mc["t"] < 300 and _mc["d"]: return _mc["d"]
    raw=urllib.request.urlopen(MANIFEST, timeout=15).read()
    _mc["d"]=json.loads(raw); _mc["t"]=time.time()
    _mc["at"]=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return _mc["d"]

# ---- rate limit (per IP, daily) ----
def rate_ok(ip):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        f=open(FP_STORE,"a+"); fcntl.flock(f,fcntl.LOCK_EX); f.seek(0)
        try: d=json.loads(f.read() or "{}")
        except Exception: d={}
        if d.get("day")!=today: d={"day":today,"counts":{}}
        n=d["counts"].get(ip,0)+1; d["counts"][ip]=n
        f.seek(0); f.truncate(); f.write(json.dumps(d)); f.flush()
        fcntl.flock(f,fcntl.LOCK_UN); f.close()
        return n<=FP_LIMIT
    except Exception:
        return True

VERDICT_MEANING = {
 "PAYABLE": ("well formed 402 with at least one settleable option", None),
 "NOT_PAYABLE": ("402 parsed but no option can settle, for example a Solana payTo without an ATA",
                 "check pay_to on each option; for Solana create the USDC ATA"),
 "MALFORMED_402": ("402 returned but neither body nor PAYMENT-REQUIRED header parses as x402 accepts",
                   "return a valid x402 challenge: accepts[] with scheme, network, payTo, asset, amount"),
 "CANNOT_PAY_EITHER_VERB": ("answered on GET and POST but never with a 402",
                            "the paid route must return 402 before payment on the declared method"),
 "UNREACHABLE": ("no response within the probe budget", "check DNS, TLS and uptime; then re-probe"),
 "SKIPPED": ("not checkable, for example a template URL with unresolved placeholders",
             "publish a concrete example URL"),
}

USDC_ASSETS = {
 ("eip155:8453","0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"): ("USDC",6),
 ("solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp","epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v"): ("USDC",6),
}

def _amount_human(network, asset, amount_raw):
    if not amount_raw: return None
    key=((network or "").lower(), (asset or "").lower())
    if key not in USDC_ASSETS: return None
    name,dec=USDC_ASSETS[key]
    try: return f"{int(amount_raw)/(10**dec):g} {name}"
    except Exception: return None

def _price(resource):
    c=_db(); r=c.execute("SELECT * FROM prices WHERE resource=?", (resource,)).fetchone(); c.close()
    if not r: return None
    ah=_amount_human(r["network"], r["asset"], r["amount_raw"])
    return {"scheme": r["scheme"], "network": r["network"], "asset": r["asset"],
            "amount_raw": r["amount_raw"], "amount_human": ah, "pay_to": r["pay_to"],
            "max_timeout": r["max_timeout"], "n_accepts": r["n_accepts"], "observed_at": r["observed_at"],
            "price_note": f"first accept of the 402 challenge as observed on {(r['observed_at'] or '')[:10]}; raw amount in atomic units; live price may differ"}

_ensure_db()
mcp = FastMCP("nsgoods-workbench", host=os.environ.get("WORKBENCH_HOST", "127.0.0.1"), port=4036, streamable_http_path="/mcp")

@mcp.tool()
def payability_verdict(url: str) -> dict:
    """Latest payability observation for one exact resource URL (path and query included), with the
    meaning of the verdict, remediation if it cannot be paid, and the per-network options seen."""
    c=_db()
    r=c.execute("SELECT * FROM resources WHERE resource=?", (url,)).fetchone()
    if not r:
        c.close()
        return {"found": False, "note": "not in catalogue (exact resource URL, path and query, must match a scanned x402 endpoint). Try find_endpoints(<fragment>)."}
    meaning, remediation = VERDICT_MEANING.get(r["last_verdict"], ("", None))
    row=c.execute("SELECT options FROM verdicts WHERE resource=? AND scan_id=? LIMIT 1",
                  (url, r["last_scan_id"])).fetchone()
    c.close()
    opts=[]
    if row and row["options"]:
        try: opts=json.loads(row["options"])
        except Exception: opts=[]
    return _stamp({"found": True, "resource": r["resource"], "host": r["host"],
            "last_verdict": r["last_verdict"], "verdict_meaning": meaning, "remediation": remediation,
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "last_scan_id": r["last_scan_id"], "verdict_changes": r["changes"], "options": opts, "price": _price(url)})

@mcp.tool()
def find_endpoints(query: str, limit: int = 25, sort: str = "") -> dict:
    """Search the catalogue by host or URL substring. Returns up to `limit` endpoints with their latest
    verdict, plus the total match count. Use this first when you do not know the exact resource URL."""
    query=(query or "").strip()
    if not query: return {"note": "give a host or URL fragment"}
    limit=max(1, min(int(limit or 25), 50))
    like="%"+query+"%"
    c=_db()
    total=c.execute("SELECT COUNT(*) n FROM resources WHERE host LIKE ? OR resource LIKE ?", (like,like)).fetchone()["n"]
    fetch_limit = 500 if sort=="price" else limit
    rows=c.execute("""SELECT r.resource resource, r.host host, r.last_verdict last_verdict,
                             r.last_seen last_seen, r.changes changes, h.gone_since gone_since,
                             p.network pnet, p.asset passet, p.amount_raw pamt
                      FROM resources r LEFT JOIN hosts h ON r.host=h.host
                      LEFT JOIN prices p ON r.resource=p.resource
                      WHERE r.host LIKE ? OR r.resource LIKE ? ORDER BY r.host, r.resource LIMIT ?""",(like,like,fetch_limit)).fetchall()
    c.close()
    eps=[]
    for x in rows:
        ah=_amount_human(x["pnet"], x["passet"], x["pamt"])
        price=None; pk=float("inf")
        if x["pamt"] is not None and x["pnet"]:
            price=f"{ah or x['pamt']} on {x['pnet']}"
            if ah:
                try: pk=float(ah.split()[0])
                except Exception: pk=float("inf")
        eps.append({"resource":x["resource"],"host":x["host"],"last_verdict":x["last_verdict"],
                    "last_seen":x["last_seen"],"verdict_changes":x["changes"],
                    "host_gone_since":x["gone_since"],"price":price,"_pk":pk})
    if sort=="price":
        eps.sort(key=lambda e:e["_pk"])
        eps=eps[:limit]
    for e in eps: e.pop("_pk", None)
    return _stamp({"query": query, "total_matches": total, "returned": len(eps), "endpoints": eps})

def _priced_count():
    c=_db(); n=c.execute("SELECT COUNT(*) n FROM prices").fetchone()["n"]; c.close(); return n

def _median_price_usdc():
    c=_db(); rows=c.execute("SELECT network,asset,amount_raw FROM prices WHERE amount_raw IS NOT NULL").fetchall(); c.close()
    vals=[]
    for r in rows:
        ah=_amount_human(r["network"], r["asset"], r["amount_raw"])
        if ah:
            try: vals.append(float(ah.split()[0]))
            except Exception: pass
    if len(vals)<100: return None
    vals.sort(); m=vals[len(vals)//2] if len(vals)%2 else (vals[len(vals)//2-1]+vals[len(vals)//2])/2
    return round(m,6)

@mcp.tool()
def catalogue_stats() -> dict:
    """Size and health of the x402 catalogue as of the latest full scan: resources, hosts, verdict
    counts, and payable endpoints per network."""
    m=_meta(); last=m.get("last_full_scan_id")
    c=_db()
    nres=c.execute("SELECT COUNT(*) n FROM verdicts WHERE scan_id=?", (last,)).fetchone()["n"]
    nhost=c.execute("SELECT COUNT(DISTINCT host) n FROM verdicts WHERE scan_id=?", (last,)).fetchone()["n"]
    vc={row["verdict"]:row["n"] for row in c.execute("SELECT verdict, COUNT(*) n FROM verdicts WHERE scan_id=? GROUP BY verdict",(last,))}
    net={}
    for row in c.execute("SELECT payable_networks FROM verdicts WHERE scan_id=? AND verdict='PAYABLE'", (last,)):
        try:
            for n in json.loads(row["payable_networks"] or "[]"): net[n]=net.get(n,0)+1
        except Exception: pass
    c.close()
    top=sorted(net.items(), key=lambda kv:-kv[1])[:10]
    man=_manifest(); pay=man.get("payability",{})
    latest_report=None
    if pay:
        k=sorted(pay.keys())[-1]; latest_report=pay[k].get("html")
    return _stamp({"resources": nres, "hosts": nhost, "verdict_counts": vc,
            "payable_by_network_top10": [{"network":n,"payable_endpoints":c2} for n,c2 in top],
            "public_aggregate": PAYABILITY_INDEX, "weekly_report": latest_report,
            "priced_resources": _priced_count(), "median_price_usdc": _median_price_usdc()})

@mcp.tool()
def host_summary(host: str) -> dict:
    """Summary for a host (no time series): first/last seen, current verdict mix, n_resources, total
    verdict changes, gone_since if absent from the latest full scan, and a small resources_sample."""
    c=_db(); h=c.execute("SELECT * FROM hosts WHERE host=?", (host,)).fetchone()
    if not h: c.close(); return {"found": False, "note": "host not in catalogue"}
    changes=c.execute("SELECT COALESCE(SUM(changes),0) s FROM resources WHERE host=?", (host,)).fetchone()["s"]
    m=_meta(); last_full=m.get("last_full_scan_id")
    in_last=c.execute("SELECT 1 FROM verdicts WHERE host=? AND scan_id=? LIMIT 1",(host,last_full)).fetchone()
    gone_since=None if in_last else h["last_seen"][:10]
    sample=[{"resource":x["resource"],"last_verdict":x["last_verdict"]}
            for x in c.execute("SELECT resource,last_verdict FROM resources WHERE host=? ORDER BY resource LIMIT 5",(host,))]
    c.close()
    return _stamp({"found": True, "host": host, "first_seen": h["first_seen"], "last_seen": h["last_seen"],
            "n_resources": h["n_resources"], "n_payable_last_full": h["n_payable_last"],
            "current_verdict_mix": json.loads(h["last_verdict_mix"] or "{}"),
            "verdict_changes_total": changes, "gone_since": gone_since, "latest_full_scan": last_full,
            "resources_sample": sample, "hint": "call find_endpoints(host) for the full list"})

@mcp.tool()
def x401_status() -> dict:
    """Current x401 emitter adoption across the scanned catalogue (from the daily watcher)."""
    try:
        d=json.load(open(WATCH_X401))
    except Exception:
        return _stamp({"note": "no x401 state file configured (set WORKBENCH_X401)"})
    return _stamp({"scanned_at": d.get("scanned_at"), "emitter_count": d.get("emitter_count"),
            "emitters": d.get("emitters", [])})

@mcp.tool()
def drift_status(host: str = "") -> dict:
    """Declared-model drift watch. With a host, that host's tracked resources; otherwise a summary."""
    try:
        d=json.load(open(WATCH_DRIFT)); res=d.get("resources", {})
    except Exception:
        return _stamp({"note": "no drift state file configured (set WORKBENCH_DRIFT)"})
    if host:
        hits={k:{"http_status":v.get("http_status"),"status":v.get("status"),"claims":v.get("claims")}
              for k,v in res.items() if host in k}
        return _stamp({"generated_at": d.get("generated_at"), "host": host, "matched": len(hits),
                       "resources": dict(list(hits.items())[:50])})
    return _stamp({"generated_at": d.get("generated_at"), "host_count": d.get("host_count"),
            "resource_count": d.get("resource_count")})

@mcp.tool()
def verify_signature(response_json: str, service: str = "") -> dict:
    """Offline EIP-191 verify of a signed nsgoods response. Identifies the service from the
    manifest (or the optional `service` arg), strips exactly that service's post-sign fields,
    canonicalises (JCS, both ASCII modes), recovers the signer and checks it against the
    published manifest signers. Reproduction-attestation (JCS/byte-length) and envelope
    (signer/signature, components) shapes are reported as unsupported_shape, not verified here.
    Refusals carry a distinct status: malformed_signature (signature is not a 65-byte 0x-prefixed
    hex string), signature_mismatch (well-formed but recovers a different address than signed_by
    under every candidate service recipe of this signer), unsupported_shape, or the
    no-signed_by/signature-pair case."""
    try:
        d=json.loads(response_json) if isinstance(response_json,str) else dict(response_json)
    except Exception as e:
        return {"status":"error","valid":False,"note":f"input is not valid JSON: {e}"}
    man=_manifest(); signers=man.get("signers",{}); at=_mc.get("at")
    _REQUIRED={"signals":{"signal","pair","timeframe"},"trust":{"result"},
        "sanctions":{"verdict","sanctioned","sdn_snapshot_at"},
        "screen-multi":{"any_list_match","list_health","lists"},
        "payable":{"returns_402","payable_networks","options"},
        "payable-address":{"classification","transfer_path","proxy_indication"}}
    _NEGATIVE={"sanctions":{"any_list_match","list_health","lists"}}
    FLAT={}
    for s in man.get("services",[]):
        pv=s.get("preview",{}) or {}; v=pv.get("verify","") or ""
        if pv.get("preview_signed") and "signed_by" in v and "recover == signed_by" in v:
            FLAT[s["name"]]={"signer":s["signer"].lower(),"post_sign":pv.get("post_sign_fields",[]) or []}
    def is_att(x): return isinstance(x,dict) and "payload" in x and "scheme" in x and ("jcs_sha256" in x or "jcs_len" in x)
    def rec_flat(x,post,sig):
        body={k:v for k,v in x.items() if k not in set(post)|{"signature","signed_by","_proof"}}
        for ea,label in ((True,"ensure_ascii=True"),(False,"ensure_ascii=False")):
            try:
                msg=json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=ea)
                return Account.recover_message(encode_defunct(text=msg),signature=sig),label
            except Exception: continue
        return None,"none matched"
    _ROOT="0x57fF0F084Cba33e6761503f90eEF0Da9F159350c"
    def _anchor(signer,service):
        # B10 part B: anchor a valid flat signature to the cross-signing root.
        # The root itself is self-asserted until its own on-chain anchor (part A).
        if signer.lower()==_ROOT.lower():
            return {"via":"self_asserted","root":_ROOT,"onchain":False}
        auth=man.get("signer_authorizations")
        if not isinstance(auth,dict):
            return {"via":"cross_signed_by","root":_ROOT,"authorization_ok":False,"reason":"no signer_authorizations in manifest"}
        try:
            abody={k:v for k,v in auth.items() if k!="signature"}
            amsg=json.dumps(abody,sort_keys=True,separators=(",",":"),ensure_ascii=True)
            arec=Account.recover_message(encode_defunct(text=amsg),signature=auth.get("signature",""))
        except Exception as e:
            return {"via":"cross_signed_by","root":_ROOT,"authorization_ok":False,"reason":f"authorization signature does not recover: {e}"}
        if arec.lower()!=_ROOT.lower() or (auth.get("root","") or "").lower()!=_ROOT.lower():
            return {"via":"cross_signed_by","root":_ROOT,"authorization_ok":False,"reason":"authorization not signed by root"}
        now=datetime.now(timezone.utc)
        for e in auth.get("authorizes",[]) or []:
            if (e.get("address","") or "").lower()!=signer.lower(): continue
            if service not in (e.get("services") or []): continue
            vf=e.get("valid_from"); vu=e.get("valid_until")
            try:
                if vf and datetime.fromisoformat(vf.replace("Z","+00:00"))>now: continue
                if vu and datetime.fromisoformat(vu.replace("Z","+00:00"))<=now: continue
            except Exception: continue
            return {"via":"cross_signed_by","root":_ROOT,"authorization_ok":True}
        return {"via":"cross_signed_by","root":_ROOT,"authorization_ok":False,"reason":"no valid authorization entry for this signer and service"}
    if not isinstance(d,dict):
        return {"status":"error","valid":False,"note":"input is not a JSON object","manifest_fetched_at":at}
    if is_att(d):
        return {"status":"unsupported_shape","valid":False,"checked":False,"signed_by_claimed":d.get("signed_by"),
                "manifest_fetched_at":at,"note":"reproduction attestation (RFC8785/JCS with byte-length prefix over the payload sub-object); not checked by this tool. Verify with the scheme in the manifest reproduction_attestations."}
    if ("components" in d and "envelope" in d) or "component_signature" in d:
        return {"status":"unsupported_shape","valid":False,"checked":False,"manifest_fetched_at":at,
                "note":"preflight composite (components + envelope) shape; not checked by this tool."}
    if "signer" in d and "signed_by" not in d:
        return {"status":"unsupported_shape","valid":False,"checked":False,"signer_claimed":d.get("signer"),
                "manifest_fetched_at":at,"note":"envelope signer/signature shape (settle/watchdog); not checked by this tool."}
    if "signature" not in d or "signed_by" not in d:
        return {"status":"invalid","valid":False,"checked":True,"manifest_fetched_at":at,"note":"response has no signed_by/signature pair"}
    sb=d["signed_by"]; sig=d["signature"]; slc={k.lower() for k in signers}
    if not (isinstance(sig,str) and sig[:2]=="0x" and len(sig)==132 and all(c in "0123456789abcdefABCDEF" for c in sig[2:])):
        return {"status":"malformed_signature","valid":False,"checked":False,"signed_by_claimed":sb,
                "manifest_fetched_at":at,"note":"signature is not a 65-byte 0x-prefixed hex string; cannot recover a signer"}
    if service:
        cands={service:FLAT[service]} if service in FLAT else {}
        if not cands:
            return {"status":"invalid","valid":False,"checked":True,"signed_by_claimed":sb,"manifest_fetched_at":at,"note":f"unknown or non-flat service '{service}'"}
    else:
        cands={n:i for n,i in FLAT.items() if i["signer"]==sb.lower()}
    for name,info in cands.items():
        if not _REQUIRED.get(name,set()).issubset(d.keys()): continue
        if name in _NEGATIVE and (_NEGATIVE[name] & set(d.keys())): continue
        rec,mode=rec_flat(d,info["post_sign"],sig)
        if rec and rec.lower()==sb.lower() and sb.lower() in slc:
            scopes=next((v for k,v in signers.items() if k.lower()==rec.lower()),None)
            return {"status":"valid","valid":True,"checked":True,"service":name,"signer":rec,"signed_by_claimed":sb,
                    "scopes":scopes,"in_manifest":True,"canonicalization":mode,"post_sign_stripped":info["post_sign"],
                    "anchor":_anchor(rec,name),
                    "manifest_fetched_at":at,"note":"recovered signer matches signed_by under this service's post-sign recipe and is a published manifest signer"}
    return {"status":"signature_mismatch","valid":False,"checked":True,"signer":None,"signed_by_claimed":sb,
            "in_manifest":(sb.lower() in slc),"manifest_fetched_at":at,
            "note":"signature is well-formed but recovers a different address than signed_by under every candidate service recipe of this signer; the body may be altered or signed for a different service"}

@mcp.tool()
def reports() -> dict:
    """Published weekly payability reports (from the signed manifest)."""
    man=_manifest(); pay=man.get("payability", {})
    out=[]
    for k in sorted(pay.keys()):
        e=pay[k]
        if isinstance(e,dict) and ("html" in e or "json" in e):
            out.append({"key":k,"published":e.get("published"),"html":e.get("html"),"json":e.get("json")})
    return {"count": len(out), "reports": out, "manifest_fetched_at": _mc.get("at")}

async def health(request):
    m=_meta(); c=_db()
    nres=c.execute("SELECT COUNT(*) c FROM resources").fetchone()[0]
    nhost=c.execute("SELECT COUNT(*) c FROM hosts").fetchone()[0]; c.close()
    c2=_db()
    lf=m.get("last_full_scan_id")
    lfres=c2.execute("SELECT COUNT(*) c FROM verdicts WHERE scan_id=?", (lf,)).fetchone()[0] if lf else None
    lfhost=c2.execute("SELECT COUNT(DISTINCT host) c FROM verdicts WHERE scan_id=?", (lf,)).fetchone()[0] if lf else None
    npr=c2.execute("SELECT COUNT(*) c FROM prices").fetchone()[0]; c2.close()
    return JSONResponse({"service":"nsgoods-workbench-mcp",
        "index_total_resources":nres,"index_total_hosts":nhost,
        "last_full_scan_id":lf,"last_full_resources":lfres,"last_full_hosts":lfhost,
        "priced_resources":npr,"tools":N_TOOLS,"built_at":m.get("built_at"),
        "rate_limit_per_ip_per_day":FP_LIMIT})

class RateLimit:
    def __init__(self, app): self.app=app
    async def __call__(self, scope, receive, send):
        if scope.get("type")!="http" or scope.get("method")!="POST" or scope.get("path")!="/mcp":
            return await self.app(scope, receive, send)
        msgs=[]; body=b""; more=True
        while more:
            mm=await receive(); msgs.append(mm); body+=mm.get("body",b""); more=mm.get("more_body",False)
        method=None
        try: method=json.loads(body).get("method")
        except Exception: pass
        if method=="tools/call":
            xff=None
            for hk,hv in scope.get("headers",[]):
                if hk==b"x-forwarded-for": xff=hv.decode().split(",")[0].strip(); break
            ip=xff or (scope.get("client") or ["?"])[0]
            if not rate_ok(ip):
                await send({"type":"http.response.start","status":429,
                    "headers":[(b"content-type",b"application/json")]})
                await send({"type":"http.response.body",
                    "body":json.dumps({"error":"free_daily_limit","limit":FP_LIMIT}).encode()})
                return
        it=iter(msgs)
        async def receive2():
            try: return next(it)
            except StopIteration: return await receive()
        await self.app(scope, receive2, send)

app = mcp.streamable_http_app()
app.router.routes.append(Route("/health", health, methods=["GET"]))
app = RateLimit(app)

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("WORKBENCH_HOST", "127.0.0.1"), port=4036, log_level="warning")
