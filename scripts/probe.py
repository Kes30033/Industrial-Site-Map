"""Fetch a list of URLs and save what comes back, for checking data sources.

Reads scripts/probe_urls.txt (one "name | url" per line) and writes each
response to probe_out/<name>.<ext> (files over 15 MB are only measured),
plus probe_out/_summary.tsv with status, size and content type.
"""
import pathlib
import time
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_SAVE = 15 * 1024 * 1024

root = pathlib.Path(__file__).resolve().parent.parent
out = root / "probe_out"
out.mkdir(exist_ok=True)
rows = []
for line in (root / "scripts" / "probe_urls.txt").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name, url = [p.strip() for p in line.split("|", 1)]
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=120) as r:
            ctype = r.headers.get("Content-Type", "")
            data = r.read(MAX_SAVE + 1)
            status = r.status
        ext = ("html" if "html" in ctype else "json" if "json" in ctype
               else "csv" if "csv" in ctype else "bin")
        size = len(data)
        if size <= MAX_SAVE:
            (out / f"{name}.{ext}").write_bytes(data)
        else:
            size = f">{MAX_SAVE}"
        rows.append(f"{name}\t{status}\t{size}\t{ctype}\t{time.time()-t0:.1f}s\t{url}")
    except Exception as e:  # record and keep going
        rows.append(f"{name}\tERR\t0\t{type(e).__name__}: {e}\t{time.time()-t0:.1f}s\t{url}")
    print(rows[-1], flush=True)

(out / "_summary.tsv").write_text("\n".join(rows) + "\n")
