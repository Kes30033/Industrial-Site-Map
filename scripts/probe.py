"""Fetch a list of URLs and save what comes back, for checking data sources.

Reads scripts/probe_urls.txt (one "name | url [| user-agent]" per line) and
writes each response to probe_out/<name>.<ext>. Files over 15 MB are measured
(streamed and counted) and only their first 64 KB is kept as <name>.head.
probe_out/_summary.tsv records status, size and content type.
"""
import pathlib
import time
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
MAX_SAVE = 15 * 1024 * 1024
MAX_COUNT = 600 * 1024 * 1024

root = pathlib.Path(__file__).resolve().parent.parent
out = root / "probe_out"
out.mkdir(exist_ok=True)
rows = []
for line in (root / "scripts" / "probe_urls.txt").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    parts = [p.strip() for p in line.split("|")]
    name, url = parts[0], parts[1]
    ua = parts[2] if len(parts) > 2 and parts[2] else UA
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=180) as r:
            ctype = r.headers.get("Content-Type", "")
            status = r.status
            buf = bytearray()
            total = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if len(buf) <= MAX_SAVE:
                    buf.extend(chunk)
                if total > MAX_COUNT:
                    break
        ext = ("html" if "html" in ctype else "json" if "json" in ctype
               else "csv" if "csv" in ctype else "txt" if "text/plain" in ctype
               else "bin")
        if total <= MAX_SAVE:
            (out / f"{name}.{ext}").write_bytes(bytes(buf))
            size = total
        else:
            (out / f"{name}.head").write_bytes(bytes(buf[:65536]))
            size = f"{total}{'+' if total > MAX_COUNT else ''} (head only)"
        rows.append(f"{name}\t{status}\t{size}\t{ctype}\t{time.time()-t0:.1f}s\t{url}")
    except Exception as e:  # record and keep going
        rows.append(f"{name}\tERR\t0\t{type(e).__name__}: {e}\t{time.time()-t0:.1f}s\t{url}")
    print(rows[-1], flush=True)

(out / "_summary.tsv").write_text("\n".join(rows) + "\n")
