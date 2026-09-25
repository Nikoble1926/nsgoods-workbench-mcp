<p align="center">
  <img src="https://mcp.nsgoods.org/logo.png" alt="nsgoods" width="120">
</p>

<h1 align="center">nsgoods Workbench MCP</h1>

<p align="center">Read only index of the weekly x402 catalogue scan. Which endpoints are payable, what they cost, which hosts are gone.<br>No wallet. No sign in. No payments handled by this server.</p>

<p align="center">
  <a href="https://mcp.nsgoods.org/"><img src="https://img.shields.io/badge/endpoint-mcp.nsgoods.org%2Fmcp-111111" alt="endpoint"></a>
  <a href="https://registry.modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP%20Registry-org.nsgoods%2Fnsgoods--workbench--mcp-111111" alt="MCP Registry"></a>
  <a href="https://smithery.ai/servers/nikosble1926/nsgoods-workbench"><img src="https://img.shields.io/badge/Smithery-listed-111111" alt="Smithery"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-111111" alt="MIT"></a>
</p>

## What it answers

A hosted Model Context Protocol server over our weekly full scan of the x402 catalogue: is this endpoint payable, what does it cost, which host is gone, which endpoints match a keyword, and whether a signed nsgoods response verifies offline.

Hosted endpoint: `https://mcp.nsgoods.org/mcp` (Streamable HTTP, no sign in, 300 tool calls per IP per day). Landing and docs: https://mcp.nsgoods.org/ · Health: https://mcp.nsgoods.org/health

Data is point in time from the last full scan. Every answer carries `as_of` and the scan id. For a live check of one endpoint before paying, use the paid `/payable` oracle on the hub. This server is the index.

## Connect

**Claude** (web or desktop)
1. Settings, then Connectors, then Add custom connector
2. Name: nsgoods. URL: `https://mcp.nsgoods.org/mcp`. No sign in.
3. New conversation, then ask for example: *Using nsgoods, find the cheapest payable endpoint for sanctions screening on Base*

**Cursor**: add to `mcp.json`
```json
{"mcpServers":{"nsgoods":{"url":"https://mcp.nsgoods.org/mcp"}}}
```

**Smithery**: https://smithery.ai/servers/nikosble1926/nsgoods-workbench

Any MCP client that supports Streamable HTTP works the same way.

## Tools
| tool | what it answers |
| --- | --- |
| find_endpoints | search the catalogue by host or keyword, with the latest verdict and the observed price per endpoint, optional sort by price |
| payability_verdict | verdict, history, remediation hint and price for one exact URL |
| catalogue_stats | size of the catalogue, verdict counts, payable endpoints per network, median price |
| host_summary | per host verdict mix, first and last seen, gone since |
| drift_status | verdict changes between scans |
| x401_status | current x401 emitter count |
| verify_signature | verify any nsgoods signed response offline against our manifest |
| reports | links to the weekly payability reports |

All tools are read only and idempotent (declared through MCP tool annotations).

Verdicts: `PAYABLE`, `NOT_PAYABLE`, `MALFORMED_402`, `CANNOT_PAY_EITHER_VERB`, `UNREACHABLE`, `SKIPPED`, `UNKNOWN`.

## Example
A live `find_endpoints("erc20-balance", 2)` call, trimmed to two rows:

```json
{
  "query": "erc20-balance",
  "total_matches": 6,
  "returned": 2,
  "endpoints": [
    {
      "resource": "https://api.onesource.io/api/chain/erc20-balance",
      "host": "api.onesource.io",
      "last_verdict": "PAYABLE",
      "host_gone_since": null,
      "price": "0.003 USDC on eip155:8453"
    },
    {
      "resource": "https://app.heinrichstech.com/v1/cdp/chain/erc20-balance",
      "host": "app.heinrichstech.com",
      "last_verdict": "PAYABLE",
      "host_gone_since": null,
      "price": "0.003 USDC on eip155:8453"
    }
  ],
  "as_of": "2026-09-13",
  "index_scan_id": "scan-20260913T050332Z"
}
```

## Data and freshness
Index is rebuilt after each weekly full scan (14,652 resources, 1,971 hosts, 13,146 payable as of 13 September 2026). Prices come from a sweep of the 402 challenges (10,958 endpoints priced, median 0.01 USDC, 16 September 2026). A per endpoint sample with a GET only re-probe outcome per row is published under CC BY 4.0: https://x402.nsgoods.org/proof/payability-sample-2026-09-13.json

## Run your own
````
python -m venv venv && venv/bin/pip install -r requirements.txt
````
Put your own scans.jsonl (one JSON line per probe, see build_index.py for the fields) next to the scripts, then:
````
venv/bin/python build_index.py
venv/bin/python server.py   # listens on 127.0.0.1:4036, path /mcp
````
Docker:
````
docker build -t nsgoods-workbench-mcp . && docker run -p 4036:4036 nsgoods-workbench-mcp
````
Docker image is untested (built without a Docker host). Set WORKBENCH_HOST=0.0.0.0 inside containers. Feedback welcome.

Environment variables:

| var | default | meaning |
| --- | --- | --- |
| WORKBENCH_DIR | . | base directory for the index and data files |
| WORKBENCH_DATA_DIR | WORKBENCH_DIR | directory for scans.jsonl and watch state |
| WORKBENCH_HOST | 127.0.0.1 | bind address |

The server creates an empty index on first run if none is present, so tools/list works before you load any data.

## Verification
Signed nsgoods responses use EIP-191 over canonical JSON (sorted keys, compact separators, ASCII escaped, signature and signed_by removed before hashing). The verify_signature tool checks the recovered address against the signers in https://x402.nsgoods.org/proof/index.json

Verifying what runs. `python3 scripts/pin_verify_signature.py server.py` prints the line range, byte range and sha256 of the verify_signature function body (def to end, decorators excluded, LF line endings, no trailing newline) and the sha256 of the whole file. A matching hash is repo evidence only: it shows that the published source is the one described. It is not deployment evidence. The stronger evidence for what the server runs is behavioural: calling the live tools and checking that the results match what this source produces.

## Links
Landing https://mcp.nsgoods.org/ · Hub https://x402.nsgoods.org · MCP section https://x402.nsgoods.org/#mcp
Weekly report https://x402.nsgoods.org/proof/payability-report-2026-09-13.html
Methodology https://x402.nsgoods.org/ata-audit/payability_index.json (method field)

MIT license.
