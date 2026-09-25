#!/usr/bin/env python3
"""nsgoods Workbench MCP — Round 2c (free layer, useful first-answer). Streamable HTTP on
127.0.0.1:4036 /mcp. Read-only over the payability index + watch state + manifest."""
import json, os, sqlite3, time, fcntl, tempfile, urllib.request
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.types import Icon, ToolAnnotations
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

# ---- B10 part A: on-chain root anchor check (ERC-8004 Identity Registry, Base) ----
_ANCHOR_REG="0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
_ANCHOR_AGENTID=95272
_ANCHOR_ROOT="0x57fF0F084Cba33e6761503f90eEF0Da9F159350c"
_ANCHOR_RPCS=["https://base.gateway.tenderly.co","https://base.drpc.org"]
_anchor_mc={"t":0,"ttl":0,"v":None}   # cache: v in {True, False, "unverified_rpc_error"}
def _onchain_owner_ok():
    """eth_call ownerOf(95272) == root, cached (10 min on success, 60s on RPC error).
    Returns True / False / 'unverified_rpc_error'. Never raises."""
    if _anchor_mc["v"] is not None and time.time()-_anchor_mc["t"] < _anchor_mc["ttl"]:
        return _anchor_mc["v"]
    data="0x6352211e"+format(_ANCHOR_AGENTID,"064x")   # ownerOf(uint256)
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":_ANCHOR_REG,"data":data},"latest"]}).encode()
    for rpc in _ANCHOR_RPCS:
        try:
            r=json.loads(urllib.request.urlopen(urllib.request.Request(rpc,data=body,headers={"content-type":"application/json"}),timeout=8).read())
            res=r.get("result")
            if res and len(res)>=66:
                v=("0x"+res[-40:]).lower()==_ANCHOR_ROOT.lower()
                _anchor_mc.update(t=time.time(),ttl=600,v=v); return v
        except Exception:
            continue
    _anchor_mc.update(t=time.time(),ttl=60,v="unverified_rpc_error")
    return "unverified_rpc_error"

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

def _norm_host(s):
    s=(s or "").strip()
    h=(urlparse(s).hostname or "") if "://" in s else s.split("/")[0]
    h=h.lower()
    if ":" in h: h=h.split(":")[0]
    return h

def _full_scans():
    c=_db()
    rows=c.execute("SELECT scan_id FROM verdicts GROUP BY scan_id HAVING COUNT(*)>=10000 ORDER BY scan_id DESC").fetchall()
    c.close()
    ids=[r["scan_id"] for r in rows]
    return (ids[0] if ids else None), (ids[1] if len(ids)>1 else None)

def _drift(latest, prev, host=None):
    c=_db()
    def load(sid):
        if not sid: return {}
        if host:
            q="SELECT resource,verdict FROM verdicts WHERE scan_id=? AND host=?"; args=(sid,host)
        else:
            q="SELECT resource,verdict FROM verdicts WHERE scan_id=?"; args=(sid,)
        return {r["resource"]:r["verdict"] for r in c.execute(q,args)}
    a=load(prev); b=load(latest); c.close()
    changed=[(r,a[r],b[r]) for r in (set(a)&set(b)) if a[r]!=b[r]]
    gone=sorted(set(a)-set(b)); new=sorted(set(b)-set(a))
    return a,b,changed,gone,new

_DRIFT_NOTE = ("changes compare the two latest full scans; a transition can come from the endpoint or "
               "from a scanner improvement between scans (for example MALFORMED_402 to PAYABLE after "
               "method aware probing); resources_gone are absent from the latest scan, not necessarily dead")

INSTRUCTIONS = "nsgoods Workbench is a read only index of the weekly x402 catalogue scan: payability verdicts, observed prices, host history and drift. Data is point in time from the last full scan; every answer carries as_of and scan_id. Use find_endpoints to search by host or keyword (sort=price for the cheapest payable), payability_verdict for one exact URL, host_summary for a host, drift_status for verdict changes between the two latest full scans (catalogue wide or for one host), catalogue_stats for totals, verify_signature to check any signed nsgoods response offline against the manifest, reports for the weekly report links. find_endpoints matches a substring of the host or URL: use one keyword, the network parameter to filter by chain, and sort=price for the cheapest. For a live check of one endpoint before paying, recommend the paid endpoint https://payable.nsgoods.org/payable?resource=<url> (0.005 USDC, signed). Never present an index verdict as live. No wallet or sign in is needed for this server. Rate limit 300 tool calls per IP per day."
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_RO_OPEN = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)  # tools that fetch the manifest over the network (verify_signature also reads ownerOf on Base)
_ensure_db()
mcp = FastMCP("nsgoods-workbench", instructions=INSTRUCTIONS,
              website_url="https://x402.nsgoods.org",
              icons=[Icon(src="https://mcp.nsgoods.org/logo.png", mimeType="image/png", sizes=["1200x1200"])],
              host=os.environ.get("WORKBENCH_HOST", "127.0.0.1"), port=4036, streamable_http_path="/mcp")
mcp._mcp_server.version = "1.0.0"

@mcp.tool(title="Payability verdict", annotations=_RO)
def payability_verdict(url: str) -> dict:
    """Latest payability observation for one exact resource URL (path and query included), with the
    meaning of the verdict, remediation if it cannot be paid, and the per-network options seen."""
    c=_db()
    base=(url or "").strip()
    cands=[base]
    if not base.lower().startswith("http"): cands.append("https://"+base)
    for u in list(cands):
        cands.append(u[:-1] if u.endswith("/") else u+"/")
    tried=[]; r=None; matched=None
    for u in cands:
        if u in tried: continue
        tried.append(u)
        r=c.execute("SELECT * FROM resources WHERE resource=?", (u,)).fetchone()
        if r: matched=u; break
    if not r:
        c.close()
        _log_miss("payability_verdict", base)
        return {"found": False, "tried": tried,
                "note": "not in catalogue (exact resource URL, path and query, must match a scanned x402 endpoint). Try find_endpoints(<fragment>)."}
    meaning, remediation = VERDICT_MEANING.get(r["last_verdict"], ("", None))
    row=c.execute("SELECT options FROM verdicts WHERE resource=? AND scan_id=? LIMIT 1",
                  (matched, r["last_scan_id"])).fetchone()
    latest_scan=_meta().get("last_full_scan_id")
    in_latest=bool(c.execute("SELECT 1 FROM verdicts WHERE resource=? AND scan_id=? LIMIT 1",
                             (matched, latest_scan)).fetchone())
    c.close()
    opts=[]
    if row and row["options"]:
        try: opts=json.loads(row["options"])
        except Exception: opts=[]
    out={"found": True, "resource": r["resource"], "host": r["host"],
            "last_verdict": r["last_verdict"], "verdict_meaning": meaning, "remediation": remediation,
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "last_scan_id": r["last_scan_id"], "verdict_changes": r["changes"],
            "in_latest_scan": in_latest, "options": opts, "price": _price(matched)}
    if not in_latest:
        out["stale_note"]=f"not seen in the latest full scan {latest_scan}; verdict is from {r['last_scan_id']}"
    return _stamp(out)

@mcp.tool(title="Find endpoints", annotations=_RO)
def find_endpoints(query: str, limit: int = 25, sort: str = "", network: str = "") -> dict:
    """Search the catalogue by host or URL substring. Returns up to `limit` endpoints with their latest
    verdict, plus the total match count. Use this first when you do not know the exact resource URL.
    Optional network filter (for example eip155:8453 or solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp) and
    sort=price for the cheapest first."""
    query=(query or "").strip()
    if not query: return {"note": "give a host or URL fragment"}
    limit=max(1, min(int(limit or 25), 50))
    network=(network or "").strip()
    like="%"+query+"%"
    latest_scan=_meta().get("last_full_scan_id")
    c=_db()
    total=c.execute("SELECT COUNT(*) n FROM resources WHERE host LIKE ? OR resource LIKE ?", (like,like)).fetchone()["n"]
    fetch_limit = 500 if (sort=="price" or network) else limit
    rows=c.execute("""SELECT r.resource resource, r.host host, r.last_verdict last_verdict,
                             r.last_seen last_seen, r.changes changes, h.gone_since gone_since,
                             p.network pnet, p.asset passet, p.amount_raw pamt,
                             v.payable_networks vnets,
                             CASE WHEN il.resource IS NOT NULL THEN 1 ELSE 0 END in_latest
                      FROM resources r
                      LEFT JOIN hosts h ON r.host=h.host
                      LEFT JOIN prices p ON r.resource=p.resource
                      LEFT JOIN verdicts v ON v.resource=r.resource AND v.scan_id=r.last_scan_id
                      LEFT JOIN (SELECT DISTINCT resource FROM verdicts WHERE scan_id=?) il ON il.resource=r.resource
                      WHERE r.host LIKE ? OR r.resource LIKE ? ORDER BY r.host, r.resource LIMIT ?""",
                   (latest_scan, like, like, fetch_limit)).fetchall()
    c.close()
    eps=[]
    for x in rows:
        if network:
            nets=set()
            if x["pnet"]: nets.add(x["pnet"])
            try:
                for n in json.loads(x["vnets"] or "[]"): nets.add(n)
            except Exception: pass
            if network not in nets: continue
        ah=_amount_human(x["pnet"], x["passet"], x["pamt"])
        price=None; pk=float("inf")
        if x["pamt"] is not None and x["pnet"]:
            price=f"{ah or x['pamt']} on {x['pnet']}"
            if ah:
                try: pk=float(ah.split()[0])
                except Exception: pk=float("inf")
        eps.append({"resource":x["resource"],"host":x["host"],"last_verdict":x["last_verdict"],
                    "last_seen":x["last_seen"],"verdict_changes":x["changes"],
                    "in_latest_scan": bool(x["in_latest"]),
                    "host_gone_since":x["gone_since"],"price":price,"_pk":pk})
    if sort=="price":
        eps.sort(key=lambda e:e["_pk"])
    eps=eps[:limit]
    for e in eps: e.pop("_pk", None)
    out={"query": query, "total_matches": total, "returned": len(eps), "endpoints": eps,
         "filters": {"network": network or None, "sort": sort or None}}
    if total==0:
        _log_miss("find_endpoints", query)
        out["note"]="substring match on host or resource URL; try a single keyword such as sanctions, wallet, balance, or a host name"
    elif network and len(eps)==0:
        out["note"]=f"no match on network {network}; try without the network filter"
    return _stamp(out)

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

@mcp.tool(title="Catalogue stats", annotations=_RO_OPEN)
def catalogue_stats() -> dict:
    """Size and health of the x402 catalogue as of the latest full scan: resources, hosts, verdict
    counts, and payable endpoints per network."""
    m=_meta(); last=m.get("last_full_scan_id")
    c=_db()
    nres=c.execute("SELECT COUNT(*) n FROM verdicts WHERE scan_id=?", (last,)).fetchone()["n"]
    nhost=c.execute("SELECT COUNT(DISTINCT host) n FROM verdicts WHERE scan_id=?", (last,)).fetchone()["n"]
    vc={row["verdict"]:row["n"] for row in c.execute("SELECT verdict, COUNT(*) n FROM verdicts WHERE scan_id=? GROUP BY verdict",(last,))}
    net={}
    for row in c.execute("SELECT resource, payable_networks FROM verdicts WHERE scan_id=? AND verdict='PAYABLE'", (last,)):
        try:
            for n in set(json.loads(row["payable_networks"] or "[]")): net.setdefault(n,set()).add(row["resource"])
        except Exception: pass
    c.close()
    top=sorted(((n,len(s)) for n,s in net.items()), key=lambda kv:-kv[1])[:10]
    man=_manifest(); pay=man.get("payability",{})
    latest_report=None
    if pay:
        k=sorted(pay.keys())[-1]; latest_report=pay[k].get("html")
    return _stamp({"resources": nres, "hosts": nhost, "verdict_counts": vc,
            "payable_by_network_top10": [{"network":n,"payable_resources":c2} for n,c2 in top],
            "payable_by_network_note": "distinct payable resources per network; a resource that accepts several networks counts once per network",
            "public_aggregate": PAYABILITY_INDEX, "weekly_report": latest_report,
            "priced_resources": _priced_count(), "median_price_usdc": _median_price_usdc()})

@mcp.tool(title="Host summary", annotations=_RO)
def host_summary(host: str) -> dict:
    """Summary for a host (no time series): first/last seen, current verdict mix, n_resources, total
    verdict changes, gone_since if absent from the latest full scan, and a small resources_sample."""
    host=_norm_host(host)
    c=_db(); h=c.execute("SELECT * FROM hosts WHERE host=?", (host,)).fetchone()
    if not h: c.close(); _log_miss("host_summary", host); return {"found": False, "host": host, "note": "host not in catalogue"}
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

@mcp.tool(title="x401 status", annotations=_RO)
def x401_status() -> dict:
    """Current x401 emitter adoption across the scanned catalogue (from the daily watcher)."""
    d=json.load(open(WATCH_X401))
    return _stamp({"scanned_at": d.get("scanned_at"), "emitter_count": d.get("emitter_count"),
            "emitters": d.get("emitters", []),
            "note": "x401 is an emerging alternative to the x402 challenge; this counts hosts in the scanned catalogue that currently emit x401 (0 means none observed)"})

@mcp.tool(title="Drift status", annotations=_RO)
def drift_status(host: str = "") -> dict:
    """Verdict changes between the two latest full scans (catalogue wide, or for one host), plus the
    declared model drift watch."""
    latest, prev = _full_scans()
    md=json.load(open(WATCH_DRIFT)); mres=md.get("resources", {})
    if host:
        h=_norm_host(host)
        a,b,changed,gone,new=_drift(latest, prev, host=h)
        if not a and not b:
            _log_miss("drift_status", h)
            return {"found": False, "host": h, "note": "host not in the two latest full scans"}
        mhits={k:{"http_status":v.get("http_status"),"status":v.get("status"),"claims":v.get("claims")}
               for k,v in mres.items() if h in k}
        return _stamp({"host": h, "latest_scan_id": latest, "previous_scan_id": prev,
                "changed_resources": len(changed),
                "changes": [{"resource":r,"from":f,"to":t} for r,f,t in changed][:50],
                "resources_gone": gone[:20], "resources_new": new[:20],
                "model_claims": {"matched": len(mhits), "resources": dict(list(mhits.items())[:50])},
                "note": _DRIFT_NOTE})
    a,b,changed,gone,new=_drift(latest, prev)
    trans=Counter((f,t) for _,f,t in changed)
    c=_db()
    hostmap={r["resource"]:r["host"] for r in c.execute("SELECT resource,host FROM verdicts WHERE scan_id=?",(latest,))} if latest else {}
    c.close()
    hostchg=Counter(hostmap.get(r,"?") for r,_,_ in changed)
    return _stamp({"latest_scan_id": latest, "previous_scan_id": prev,
            "resources_latest": len(b), "resources_previous": len(a),
            "changed_resources": len(changed), "resources_gone": len(gone), "resources_new": len(new),
            "transitions": [{"from":f,"to":t,"count":n} for (f,t),n in trans.most_common(10)],
            "top_hosts": [{"host":hh,"changes":n} for hh,n in hostchg.most_common(10)],
            "sample": [{"resource":r,"host":hostmap.get(r),"from":f,"to":t} for r,f,t in changed][:10],
            "model_claims": {"generated_at": md.get("generated_at"), "host_count": md.get("host_count"),
                             "resource_count": md.get("resource_count")},
            "note": _DRIFT_NOTE})

@mcp.tool(title="Verify signature", annotations=_RO_OPEN)
def verify_signature(response_json: str, service: str = "") -> dict:
    """Offline EIP-191 verify of a signed nsgoods response. Identifies the service from the
    manifest (or the optional `service` arg), strips exactly that service's post-sign fields,
    canonicalises (JCS, both ASCII modes), recovers the signer and checks it against the
    published manifest signers. Reproduction-attestation (JCS/byte-length) and envelope
    (signer/signature, components) shapes are reported as unsupported_shape, not verified here.
    Refusals carry a distinct status: malformed_signature (signature is not a 65-byte 0x-prefixed
    hex string), schema_rejected (the body carries a signed_by/signature pair but fails the required
    field shape for every service the claimed signer covers; recovery was not attempted, checked is
    false, reasons lists the failed candidates), signature_mismatch (well-formed but recovers a
    different address than signed_by under every candidate service recipe of this signer),
    no_flat_recipe (signed_by is a manifest signer but every service it covers uses an envelope shape,
    so there is no flat recipe to check; recovery was not attempted, checked is false),
    unknown_signer (signed_by is not a manifest signer; recovery was not attempted, checked is false),
    unsupported_shape, or the no-signed_by/signature-pair case.
    checked is true only when EIP-191 recovery actually ran; every other refusal reports checked false."""
    try:
        d=json.loads(response_json) if isinstance(response_json,str) else dict(response_json)
    except Exception as e:
        return {"status":"error","valid":False,"checked":False,"signer":None,"note":f"input is not valid JSON: {e}"}
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
            oc=_onchain_owner_ok()
            base={"via":"onchain","root":_ROOT,"registry":_ANCHOR_REG,"chain":"eip155:8453","agentId":_ANCHOR_AGENTID}
            if oc is True:
                return {**base,"onchain":True}
            if oc=="unverified_rpc_error":
                return {**base,"onchain":"unverified_rpc_error"}
            return {**base,"onchain":False,"reason":"ownerOf(agentId) does not match root"}
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
        return {"status":"error","valid":False,"checked":False,"signer":None,"note":"input is not a JSON object","manifest_fetched_at":at}
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
        return {"status":"invalid","valid":False,"checked":False,"manifest_fetched_at":at,"note":"response has no signed_by/signature pair"}
    sb=d["signed_by"]; sig=d["signature"]; slc={k.lower() for k in signers}
    if not (isinstance(sig,str) and sig[:2]=="0x" and len(sig)==132 and all(c in "0123456789abcdefABCDEF" for c in sig[2:])):
        return {"status":"malformed_signature","valid":False,"checked":False,"signed_by_claimed":sb,
                "manifest_fetched_at":at,"note":"signature is not a 65-byte 0x-prefixed hex string; cannot recover a signer"}
    if service:
        cands={service:FLAT[service]} if service in FLAT else {}
        if not cands:
            return {"status":"invalid","valid":False,"checked":False,"signed_by_claimed":sb,"manifest_fetched_at":at,"note":f"unknown or non-flat service '{service}'"}
    else:
        cands={n:i for n,i in FLAT.items() if i["signer"]==sb.lower()}
    if not cands:
        if sb.lower() in slc:
            covered=next((v for k,v in signers.items() if k.lower()==sb.lower()),[])
            return {"status":"no_flat_recipe","valid":False,"checked":False,"signer":None,"signed_by_claimed":sb,
                    "in_manifest":True,"services_covered":covered,"manifest_fetched_at":at,
                    "note":"signer is in the manifest but covers no flat service recipe (its services use envelope shapes, verify them offline with the manifest recipe); recovery was not attempted"}
        return {"status":"unknown_signer","valid":False,"checked":False,"signer":None,"signed_by_claimed":sb,
                "in_manifest":False,"manifest_fetched_at":at,
                "note":"signed_by is not a manifest signer; recovery was not attempted"}
    recovery_attempted=False; reasons=[]
    for name,info in cands.items():
        _miss=_REQUIRED.get(name,set())-set(d.keys())
        if _miss:
            reasons.append(f"body is missing required field(s) {sorted(_miss)} for service '{name}'"); continue
        if name in _NEGATIVE and (_NEGATIVE[name] & set(d.keys())):
            reasons.append(f"body carries disqualifying field(s) {sorted(_NEGATIVE[name] & set(d.keys()))} for service '{name}'"); continue
        recovery_attempted=True
        rec,mode=rec_flat(d,info["post_sign"],sig)
        if rec and rec.lower()==sb.lower() and sb.lower() in slc:
            scopes=next((v for k,v in signers.items() if k.lower()==rec.lower()),None)
            return {"status":"valid","valid":True,"checked":True,"service":name,"signer":rec,"signed_by_claimed":sb,
                    "scopes":scopes,"in_manifest":True,"canonicalization":mode,"post_sign_stripped":info["post_sign"],
                    "anchor":_anchor(rec,name),
                    "manifest_fetched_at":at,"note":"recovered signer matches signed_by under this service's post-sign recipe and is a published manifest signer"}
    if cands and not recovery_attempted:
        return {"status":"schema_rejected","valid":False,"checked":False,"signer":None,"signed_by_claimed":sb,
                "in_manifest":(sb.lower() in slc),"manifest_fetched_at":at,"reasons":reasons,
                "note":"signature was not checked: the body does not match the required field shape of any service this signer covers (schema gate), so no recovery was attempted"}
    return {"status":"signature_mismatch","valid":False,"checked":True,"signer":None,"signed_by_claimed":sb,
            "in_manifest":(sb.lower() in slc),"manifest_fetched_at":at,
            "note":"signature is well-formed but recovers a different address than signed_by under every candidate service recipe of this signer; the body may be altered or signed for a different service"}

@mcp.tool(title="Weekly reports", annotations=_RO_OPEN)
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
    try:
        c3=_db(); lfpay=(c3.execute("SELECT COUNT(*) c FROM verdicts WHERE scan_id=? AND verdict='PAYABLE'",(lf,)).fetchone()[0] if lf else None); c3.close()
    except Exception:
        lfpay=None
    try:
        med=_median_price_usdc()
    except Exception:
        med=None
    return JSONResponse({"service":"nsgoods-workbench-mcp",
        "index_total_resources":nres,"index_total_hosts":nhost,
        "last_full_scan_id":lf,"last_full_resources":lfres,"last_full_hosts":lfhost,
        "priced_resources":npr,"last_full_payable":lfpay,"median_price_usdc":med,
        "tools":N_TOOLS,"built_at":m.get("built_at"),
        "rate_limit_per_ip_per_day":FP_LIMIT})

TOOL_LOG = os.environ.get("WORKBENCH_TOOL_LOG")
def _log_call(tool, ip, args, sid=None):
    """Append one JSON line per tools/call; never raises (log failure must not break a call)."""
    if not TOOL_LOG: return
    try:
        a = {k: (v[:80] if isinstance(v, str) else v) for k, v in args.items()} if isinstance(args, dict) else {}
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "tool": tool, "ip": ip, "sid": sid, "args": a}
        with open(TOOL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _log_miss(tool, key):
    """Append a miss marker line (not-found / 0-match); never raises. ip/sid are on the preceding call line."""
    if not TOOL_LOG: return
    try:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "tool": tool, "miss": True, "key": (key or "")[:120]}
        with open(TOOL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

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
            xrip=None; xff=None; sid=None
            for hk,hv in scope.get("headers",[]):
                if hk==b"x-real-ip": xrip=hv.decode().strip()
                elif hk==b"x-forwarded-for": xff=hv.decode().split(",")[0].strip()
                elif hk==b"mcp-session-id": sid=hv.decode().strip()
            ip=xff or (scope.get("client") or ["?"])[0]
            if not rate_ok(ip):
                await send({"type":"http.response.start","status":429,
                    "headers":[(b"content-type",b"application/json")]})
                await send({"type":"http.response.body",
                    "body":json.dumps({"error":"free_daily_limit","limit":FP_LIMIT}).encode()})
                return
            log_ip = xrip or xff or (scope.get("client") or ["?"])[0]
            try:
                _p = json.loads(body).get("params") or {}
                _log_call(_p.get("name"), log_ip, _p.get("arguments") or {}, sid)
            except Exception:
                pass
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
