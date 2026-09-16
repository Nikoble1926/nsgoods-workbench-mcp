# nsgoods workbench MCP

Read only. No wallet. No payments handled by this server.

A hosted Model Context Protocol server that answers questions about the x402
catalogue from our weekly full scan: is this endpoint payable, what does it
cost, which host is gone, which endpoints match a keyword, and whether a
signed nsgoods response verifies offline.

Hosted endpoint: https://mcp.nsgoods.org/mcp (Streamable HTTP, no sign in,
30 calls per IP per day). Health: https://mcp.nsgoods.org/health

## Connect from Claude
1. Settings, then Connectors, then Add custom connector
2. Name: nsgoods. URL: https://mcp.nsgoods.org/mcp. No sign in.
3. Start a new conversation and ask, for example:
   Using nsgoods, find the cheapest payable endpoint for ERC20 balance on Base

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
Index is rebuilt after each weekly full scan (14,652 resources, 1,971
hosts, 13,146 payable as of 13 September 2026). Prices come from a sweep of
the 402 challenges (10,958 endpoints priced, median 0.01 USDC, 16
September 2026). Every answer carries as_of. For a live check use the paid
/payable endpoint on the hub.

## Run your own
```
python -m venv venv && venv/bin/pip install -r requirements.txt
```
Put your own scans.jsonl (one JSON line per probe, see build_index.py for
the fields) next to the scripts, then:
```
venv/bin/python build_index.py
venv/bin/python server.py   # listens on 127.0.0.1:4036, path /mcp
```
Docker:
```
docker build -t nsgoods-workbench-mcp . && docker run -p 4036:4036 nsgoods-workbench-mcp
```
Paths are configurable with WORKBENCH_DIR and WORKBENCH_DATA_DIR (both default
to the current directory). The server creates an empty index on first run if
none is present, so tools/list works before you load any data.

## Verification
Signed nsgoods responses use EIP-191 over canonical JSON (sorted keys,
compact separators, ASCII escaped, signature and signed_by removed before
hashing). The verify_signature tool checks the recovered address against
the signers in https://x402.nsgoods.org/proof/index.json

## Links
Hub https://x402.nsgoods.org  MCP section https://x402.nsgoods.org/#mcp
Weekly report https://x402.nsgoods.org/proof/payability-report-2026-09-13.html
Methodology https://x402.nsgoods.org/ata-audit/payability_index.json (method field)

MIT license.
