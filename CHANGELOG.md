# Changelog

## 2026-09-27 stale verdicts carry their age (ref #1)
- payability_verdict, find_endpoints, and host_summary.resources_sample: every row that carries last_verdict now also carries verdict_observed_at (= last_seen) and verdict_age_days (integer days between last_seen and the index scan time). [C1]
- payability_verdict: when in_latest_scan is false and last_verdict is not PAYABLE, verdict_meaning and remediation are prefixed "Observed on <YYYY-MM-DD>, not re-checked since. May no longer apply. ". [C2]
- docstrings: find_endpoints and host_summary note that in_latest_scan=false rows carry a verdict from an earlier scan (read verdict_age_days); host_summary also documents that n_resources counts every resource ever seen for the host while current_verdict_mix and n_payable_last_full count only the latest full scan. [C3/C6]
- find_endpoints docstring documents the limit clamp (max 50); the clamp itself is unchanged. [C4]

## 2026-09-26 privacy-by-design tool logging
- Tool log stores only ts, tool, arg_names, arg_lengths, ip_h; rate limiter counts by ip_h; /health.tool_log reports fields and 30 day retention; INSTRUCTIONS gained a one sentence logging disclosure.

## 78e3c6a env-var config, _ensure_db, tool-log guard (2026-09-23)
- config: DB and state paths now read from environment; _ensure_db() creates the index on first run.
- payability_verdict: adds in_latest_scan and a stale_note when the verdict comes from an older scan; price from the canonical matched resource.
- find_endpoints: new network filter and a filters object in the response.
- catalogue_stats: counts distinct payable resources per network (key renamed to payable_resources).
- host_summary: not found now returns a structured note and emits a miss marker.
- drift_status (rewrite): computes changes between the two latest full scans from the DB; removed the drift state-file fallback.
- x401_status: removed the x401 state-file fallback.

## c10e59f annotations + pin script (2026-09-25)
- openWorldHint set on the 3 tools that fetch the manifest over the network; added scripts/pin_verify_signature.py.
