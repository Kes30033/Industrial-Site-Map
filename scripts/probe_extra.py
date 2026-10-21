"""Extra one-off checks that need more than a plain GET: the BLS OEWS map tool backend."""
import json
import pathlib
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
out = pathlib.Path(__file__).resolve().parent.parent / "probe_out"
out.mkdir(exist_ok=True)
BASE = "https://data.bls.gov/oesmap/"


def call(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://data.bls.gov", "Referer": BASE})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


log = []
try:
    years = json.loads(call("year/"))
    (out / "oes_years.json").write_text(json.dumps(years, indent=1))
    rel = years[0]["releaseDate"]
    log.append(f"years ok: {[y.get('releaseDate') for y in years][:4]}")
    menu = json.loads(call("getMenuData/" + rel))
    (out / "oes_menu.json").write_text(json.dumps(menu, indent=1)[:400000])
    log.append(f"menu keys {list(menu)}; measures {menu.get('measures')}")
    for area in ["S", "M"]:
        for tab in [False, True]:
            body = {"tabSwitch": tab, "dataTypeCode": "01", "releaseDate": rel, "occCode": "514121",
                    "areaTypeCode": area, "userId": "", "password": "", "sourceTbl": "oe_data_pub"}
            try:
                r = call("getMapData", body)
                (out / f"oes_map_{area}_{tab}.json").write_bytes(r[:2_000_000])
                d = json.loads(r)
                lst = d.get("mapAndTableDataList") or []
                log.append(f"getMapData {area} tab={tab}: {len(r)} bytes, n={len(lst)}, msg={d.get('message')!r}, first={lst[:2]}")
            except Exception as e:
                log.append(f"getMapData {area} tab={tab}: ERR {e}")
except Exception as e:
    log.append(f"oesmap ERR {e}")

(out / "_extra_log.txt").write_text("\n".join(log) + "\n")
print("\n".join(log))
