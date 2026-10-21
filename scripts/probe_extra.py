"""Extra one-off checks that need more than a plain GET."""
import json
import pathlib
import re
import urllib.request
import zipfile

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
out = pathlib.Path(__file__).resolve().parent.parent / "probe_out"
out.mkdir(exist_ok=True)


def get(url, data=None, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


log = []

# 1. QuantGov State RegData: list archive contents
try:
    p = pathlib.Path("/tmp/qg.zip")
    p.write_bytes(get("https://quantgov-bulk-downloads.s3.amazonaws.com/State-RegData-5-0_Regulations.zip"))
    z = zipfile.ZipFile(p)
    lines = [f"{i.filename}\t{i.file_size}" for i in z.infolist()]
    (out / "quantgov_list.txt").write_text("\n".join(lines))
    small = [i for i in z.infolist() if i.filename.endswith(".csv") and i.file_size < 3_000_000]
    for i in small[:6]:
        (out / ("qg_" + i.filename.replace("/", "_"))).write_bytes(z.read(i)[:200_000])
    log.append(f"quantgov ok, {len(lines)} entries")
except Exception as e:
    log.append(f"quantgov ERR {e}")

# 2. BLS API v2 POST with 30 series and no key: does it accept more than 25?
try:
    ser = [f"OEUS{st:02d}0000000000051412101" for st in [1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18,
                                                          19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33]]
    body = json.dumps({"seriesid": ser, "startyear": "2025", "endyear": "2025"}).encode()
    r = get("https://api.bls.gov/publicAPI/v2/timeseries/data/", data=body,
            headers={"Content-Type": "application/json"})
    (out / "bls_post.json").write_bytes(r)
    d = json.loads(r)
    log.append(f"bls post status={d.get('status')} msgs={d.get('message')[:3]} n={len(d.get('Results', {}).get('series', []))}")
except Exception as e:
    log.append(f"bls post ERR {e}")

# 3. OEWS map tool: page + scripts, look for data endpoints
try:
    base = "https://data.bls.gov/oesmap/"
    html = get(base).decode("utf-8", "replace")
    (out / "oesmap.html").write_text(html)
    srcs = re.findall(r'src="([^"]+\.js[^"]*)"', html)
    log.append(f"oesmap scripts: {srcs}")
    for s in srcs[:12]:
        u = s if s.startswith("http") else urllib.request.urljoin(base, s)
        try:
            js = get(u).decode("utf-8", "replace")
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", s)[-80:]
            (out / f"oesmap_js_{name}.txt").write_text(js[:3_000_000])
            hits = sorted(set(re.findall(r'["\']([^"\']*(?:api|json|data|\.do|service)[^"\']*)["\']', js)))[:80]
            log.append(f"  {u}: {len(js)} bytes; endpoint-like strings: {hits[:40]}")
        except Exception as e:
            log.append(f"  {u}: ERR {e}")
except Exception as e:
    log.append(f"oesmap ERR {e}")

(out / "_extra_log.txt").write_text("\n".join(log) + "\n")
print("\n".join(log))
