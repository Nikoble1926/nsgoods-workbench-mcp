#!/usr/bin/env python3
"""Drop tool-log lines older than RETENTION_DAYS. Atomic temp-file replace,
preserving the original file's owner and mode. Never deletes the file itself."""
import os, json, tempfile
from datetime import datetime, timezone, timedelta
LOG = os.environ.get("WORKBENCH_TOOL_LOG", "/root/workbench-mcp/tool_calls.jsonl")
RETENTION_DAYS = 30
def main():
    if not os.path.exists(LOG): return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    st = os.stat(LOG)
    d = os.path.dirname(LOG) or "."
    kept = 0; dropped = 0
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".toollog.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out, open(LOG, encoding="utf-8", errors="replace") as f:
            for ln in f:
                s = ln.strip()
                if not s:
                    continue
                try:
                    ts = json.loads(s).get("ts")
                    old = ts and datetime.fromisoformat(ts) < cutoff
                except Exception:
                    old = False          # unparseable -> keep (never silently lose data)
                if old:
                    dropped += 1
                else:
                    out.write(ln if ln.endswith("\n") else ln + "\n"); kept += 1
        os.chmod(tmp, st.st_mode & 0o777)
        try: os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError: pass
        os.replace(tmp, LOG)            # atomic
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise
if __name__ == "__main__":
    main()
