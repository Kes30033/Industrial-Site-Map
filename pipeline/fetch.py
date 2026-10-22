"""Download every source dataset into cache/.

Each source is fetched independently. If one fails, the copy already in cache/
(restored from the data-cache branch by the workflow) is kept, so a single
government site being down never breaks the build. cache/manifest.json records
where each file came from and when.

Run: python pipeline/fetch.py            (all sources)
     python pipeline/fetch.py oews qcew  (just some)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import sys
import time
import traceback
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
TODAY = dt.date.today()
YEAR = TODAY.year

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})

MANIFEST_PATH = CACHE / "manifest.json"
manifest: dict = json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}


# ----------------------------------------------------------------------------- helpers
def get(url: str, *, stream=False, timeout=180, tries=3, **kw) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = S.get(url, stream=stream, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} for {url}")
            if r.status_code in (403, 404, 410):
                break
        except requests.RequestException as e:  # network hiccup: retry
            last = e
        time.sleep(2 + 3 * i)
    raise last


def exists(url: str) -> bool:
    try:
        r = S.head(url, timeout=60, allow_redirects=True)
        if r.status_code == 405:  # some servers refuse HEAD
            r = S.get(url, timeout=60, stream=True)
            r.close()
        return r.status_code == 200
    except requests.RequestException:
        return False


def first_existing(urls):
    for u in urls:
        if exists(u):
            return u
    return None


def save(rel: str, data: bytes | str) -> Path:
    p = CACHE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_bytes(data)
    return p


def record(key: str, **info):
    info["fetched"] = TODAY.isoformat()
    manifest[key] = info
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=1, sort_keys=True))


def arcgis(layer_url: str, *, where="1=1", fields="*", offset_deg=None, precision=None,
           page=2000, fmt="geojson", extra=None) -> dict:
    """Page through an ArcGIS FeatureServer layer and return one FeatureCollection."""
    feats, start = [], 0
    while True:
        params = {"where": where, "outFields": fields, "f": fmt, "outSR": 4326,
                  "resultOffset": start, "resultRecordCount": page, "orderByFields": "OBJECTID"}
        if offset_deg:
            params["maxAllowableOffset"] = offset_deg
        if precision:
            params["geometryPrecision"] = precision
        params.update(extra or {})
        d = get(f"{layer_url}/query?{urlencode(params)}", timeout=300).json()
        if "error" in d:
            raise RuntimeError(f"ArcGIS error {d['error']} for {layer_url}")
        batch = d.get("features", [])
        feats.extend(batch)
        more = d.get("exceededTransferLimit") or (d.get("properties") or {}).get("exceededTransferLimit")
        if not batch or (len(batch) < page and not more):
            break
        start += len(batch)
        time.sleep(0.3)
    return {"type": "FeatureCollection", "features": feats}


NTAD = "https://services.arcgis.com/xOi1kZaI0eWDREZv/ArcGIS/rest/services"
GEODOT = "https://geo.dot.gov/server/rest/services/Hosted"


# ----------------------------------------------------------------------------- sources
def src_county_shapes():
    url = first_existing([f"https://www2.census.gov/geo/tiger/GENZ{y}/shp/cb_{y}_us_county_5m.zip"
                          for y in (YEAR, YEAR - 1, YEAR - 2)])
    if not url:
        raise RuntimeError("no cartographic boundary county file found")
    save("census/county_5m.zip", get(url).content)
    record("county_shapes", url=url, vintage=re.search(r"cb_(\d{4})", url).group(1),
           title="Census cartographic boundary counties (1:5,000,000)")


def src_population():
    cands = []
    for y in (YEAR, YEAR - 1, YEAR - 2):
        cands.append(f"https://www2.census.gov/programs-surveys/popest/datasets/2020-{y}/counties/totals/co-est{y}-alldata.csv")
    url = first_existing(cands)
    if not url:
        raise RuntimeError("no county population estimates file found")
    df = pd.read_csv(io.BytesIO(get(url).content), encoding="latin-1", dtype=str)
    y = re.search(r"co-est(\d{4})", url).group(1)
    keep = ["SUMLEV", "STATE", "COUNTY", "STNAME", "CTYNAME", f"POPESTIMATE{y}"]
    df = df[df.SUMLEV == "050"][keep].rename(columns={f"POPESTIMATE{y}": "POP"})
    save("census/population.csv", df.to_csv(index=False))
    record("population", url=url, vintage=y, title=f"Census county population estimates, July 1 {y}")


def src_centroids():
    url = "https://www2.census.gov/geo/docs/reference/cenpop2020/county/CenPop2020_Mean_CO.txt"
    save("census/cenpop2020_county.csv", get(url).content.decode("utf-8-sig"))
    record("cenpop", url=url, vintage="2020", title="Census 2020 county centers of population")
    gurl = first_existing([f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{y}_Gazetteer/{y}_Gaz_counties_national.zip"
                           for y in (YEAR, YEAR - 1, YEAR - 2)])
    z = zipfile.ZipFile(io.BytesIO(get(gurl).content))
    txt = z.read(z.namelist()[0]).decode("utf-8", "replace")
    save("census/gazetteer_counties.txt", txt)
    record("gazetteer", url=gurl, vintage=re.search(r"/(\d{4})_Gaz", gurl).group(1),
           title="Census Gazetteer counties (land area, internal points)")


def src_acs_occupations():
    """ACS 5-year table C24010 (occupation groups), county rows only, from the table-based summary file."""
    url = first_existing([f"https://www2.census.gov/programs-surveys/acs/summary_file/{y}/table-based-SF/data/5YRData/acsdt5y{y}-c24010.dat"
                          for y in (YEAR - 1, YEAR - 2, YEAR - 3)])
    if not url:
        raise RuntimeError("no ACS C24010 summary file found")
    r = get(url, stream=True, timeout=600)
    out = io.StringIO()
    n = 0
    header = None
    for raw in r.iter_lines():
        line = raw.decode("utf-8", "replace")
        if header is None:
            header = line
            out.write(line + "\n")
            continue
        if line.startswith("0500000US"):
            out.write(line + "\n")
            n += 1
    if n < 3000:
        raise RuntimeError(f"only {n} county rows in ACS file")
    save("census/acs_c24010_county.psv", out.getvalue())
    y = re.search(r"acsdt5y(\d{4})", url).group(1)
    record("acs_c24010", url=url, vintage=f"{int(y) - 4}-{y}", rows=n,
           title=f"ACS {int(y) - 4}-{y} 5-year estimates, table C24010 (occupation by county of residence)")


def src_cbsa():
    url = "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/delineation-files/list1_2023.xlsx"
    df = pd.read_excel(io.BytesIO(get(url).content), header=2, dtype=str)
    df = df[df["FIPS State Code"].notna()]
    save("census/cbsa_2023.csv", df.to_csv(index=False))
    record("cbsa", url=url, vintage="2023", title="OMB metropolitan area delineations (July 2023)")


QCEW_INDUSTRIES = {
    # code: (own_code, agglvl for county rows, label)
    "10": ("0", "70", "Total, all industries"),
    "31_33": ("5", "74", "Manufacturing"),
    "332": ("5", "75", "Fabricated metal product manufacturing"),
    "333": ("5", "75", "Machinery manufacturing"),
    "336": ("5", "75", "Transportation equipment manufacturing"),
    "484": ("5", "75", "Truck transportation"),
    "493": ("5", "75", "Warehousing and storage"),
    "2382": ("5", "76", "Building equipment contractors (electrical, HVAC, plumbing)"),
}


def src_qcew():
    year = None
    for y in (YEAR - 1, YEAR - 2):
        if exists(f"https://data.bls.gov/cew/data/api/{y}/a/industry/332.csv"):
            year = y
            break
    if not year:
        raise RuntimeError("no QCEW annual data found")
    rows = []
    for code, (own, agg, label) in QCEW_INDUSTRIES.items():
        url = f"https://data.bls.gov/cew/data/api/{year}/a/industry/{code}.csv"
        df = pd.read_csv(io.BytesIO(get(url).content), dtype=str)
        df = df[(df.own_code == own) & (df.agglvl_code == agg)]
        df = df[["area_fips", "industry_code", "disclosure_code", "annual_avg_estabs",
                 "annual_avg_emplvl", "avg_annual_pay", "lq_annual_avg_emplvl"]]
        rows.append(df)
        time.sleep(0.5)
    all_df = pd.concat(rows)
    save("bls/qcew_county.csv", all_df.to_csv(index=False))
    record("qcew", url=f"https://data.bls.gov/cew/data/api/{year}/a/industry/<code>.csv", vintage=str(year),
           title=f"BLS Quarterly Census of Employment and Wages, {year} annual averages (private sector, by county)",
           industries={k: v[2] for k, v in QCEW_INDUSTRIES.items()})


OEWS_OCCS = {
    "514121": "Welders, cutters, solderers & brazers",
    "514041": "Machinists",
    "519161": "CNC tool operators",
    "472111": "Electricians",
    "499041": "Industrial machinery mechanics",
    "533032": "Heavy & tractor-trailer truck drivers",
    "537062": "Laborers & freight/stock/material movers",
}
OEWS_MEASURES = {"01": "employment", "17": "lq", "13": "median_annual_wage"}


def src_oews():
    base = "https://data.bls.gov/oesmap/"
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
           "Origin": "https://data.bls.gov", "Referer": base}
    years = get(base + "year/", headers=hdr).json()
    rel = years[0]["releaseDate"]
    out = {"release": rel, "occupations": OEWS_OCCS, "measures": OEWS_MEASURES, "rows": []}
    for occ in OEWS_OCCS:
        for meas in OEWS_MEASURES:
            for area in ("S", "M"):
                body = {"tabSwitch": False, "dataTypeCode": meas, "releaseDate": rel, "occCode": occ,
                        "areaTypeCode": area, "userId": "", "password": "", "sourceTbl": "oe_data_pub"}
                for attempt in range(3):
                    try:
                        r = S.post(base + "getMapData", json=body, headers=hdr, timeout=120)
                        r.raise_for_status()
                        d = r.json()
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        time.sleep(3)
                for row in d.get("mapAndTableDataList") or []:
                    sid = row.get("seriesId") or ""
                    out["rows"].append({
                        "occ": occ, "measure": meas, "area_type": area,
                        "area_code": sid[4:11] if sid else None,
                        "area_name": (row.get("AREA_NAME") or "").strip(),
                        "state": row.get("STATE_CODE"),
                        "value": (row.get("VALUE") or "").strip(),
                        "footnote": row.get("FOOTNOTE_CODE"),
                    })
                time.sleep(0.6)
    if len(out["rows"]) < 5000:
        raise RuntimeError(f"OEWS returned only {len(out['rows'])} rows")
    save("bls/oews.json", json.dumps(out))
    record("oews", url=base, vintage=f"May {rel[:4]}",
           title=f"BLS Occupational Employment and Wage Statistics, May {rel[:4]} (state and metro/nonmetro areas)")


def src_intermodal():
    fc = arcgis(f"{NTAD}/NTAD_Intermodal_Freight_Facilities_Rail_TOFC_COFC/FeatureServer/0",
                fields="TERMINAL,CITY,STATE,PORT,RAIL_CO,EQUIPMENT,LAT,LON")
    if len(fc["features"]) < 100:
        raise RuntimeError("too few intermodal terminals")
    save("ntad/intermodal_rail.geojson", json.dumps(fc))
    record("intermodal", url=f"{NTAD}/NTAD_Intermodal_Freight_Facilities_Rail_TOFC_COFC/FeatureServer/0",
           title="BTS NTAD Intermodal Freight Facilities: Rail TOFC/COFC terminals", n=len(fc["features"]))


def src_rail_yards():
    fc = arcgis(f"{NTAD}/NTAD_Rail_Yards/FeatureServer/0",
                fields="STCNTYFIPS,RROWNER1,RROWNER1_NAME,YARDNAME", offset_deg=0.0005, precision=5)
    if len(fc["features"]) < 1000:
        raise RuntimeError("too few rail yards")
    save("ntad/rail_yards.geojson", json.dumps(fc))
    record("rail_yards", url=f"{NTAD}/NTAD_Rail_Yards/FeatureServer/0", title="BTS NTAD Rail Yards",
           n=len(fc["features"]))


def src_class1_rail():
    fc = arcgis(f"{NTAD}/NTAD_North_American_Rail_Network_Lines_Class_I_Railroads/FeatureServer/0",
                where="COUNTRY='US'", fields="RROWNER1,NET,TRACKS,PASSNGR,STATEAB",
                offset_deg=0.001, precision=4)
    if len(fc["features"]) < 20000:
        raise RuntimeError("too few Class I rail segments")
    save("ntad/class1_rail.geojson", json.dumps(fc, separators=(",", ":")))
    record("class1_rail", url=f"{NTAD}/NTAD_North_American_Rail_Network_Lines_Class_I_Railroads/FeatureServer/0",
           title="BTS NTAD North American Rail Network: Class I railroad lines", n=len(fc["features"]))


def src_interstates():
    fc = arcgis(f"{NTAD}/Eisenhower_Interstate_System/FeatureServer/0", fields="SIGN1,STFIPS",
                offset_deg=0.001, precision=4)
    if len(fc["features"]) < 20000:
        raise RuntimeError("too few interstate segments")
    save("ntad/interstates.geojson", json.dumps(fc, separators=(",", ":")))
    record("interstates", url=f"{NTAD}/Eisenhower_Interstate_System/FeatureServer/0",
           title="FHWA/BTS Eisenhower Interstate System", n=len(fc["features"]))


def src_ports():
    fc = arcgis(f"{GEODOT}/principal_ports_view/FeatureServer/0",
                fields="rank,port,type,portname,total,domestic,foreign_,imports,exports",
                offset_deg=0.005, precision=4, page=1000, extra={"orderByFields": "objectid"})
    if len(fc["features"]) < 50:
        raise RuntimeError("too few ports")
    save("ntad/principal_ports.geojson", json.dumps(fc, separators=(",", ":")))
    record("ports", url=f"{GEODOT}/principal_ports_view/FeatureServer/0",
           title="BTS/USACE Principal Ports (total tonnage)", n=len(fc["features"]))


DOCKS = "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/ArcGIS/rest/services/Docks/FeatureServer/0"


def src_docks():
    # Individual docks; PORT matches the principal port code, so ports can be placed at their docks
    fc = arcgis(DOCKS, where="FAC_TYPE='Dock'", fields="PORT,PORT_NAME,NAV_UNIT_NAME,CITY_OR_TOWN,STATE",
                precision=5)
    if len(fc["features"]) < 5000:
        raise RuntimeError("too few docks")
    save("ntad/docks.geojson", json.dumps(fc, separators=(",", ":")))
    record("docks", url=DOCKS, title="BTS/USACE Navigation Facilities: docks", n=len(fc["features"]))


def src_air_cargo():
    page_url = "https://www.faa.gov/airports/planning_capacity/passenger_allcargo_stats/passenger"
    html = get(page_url).text
    links = sorted(set(re.findall(r'href="([^"]*ARP-CY(\d{4})-all-cargo-airports\.xlsx)"', html, re.I)),
                   key=lambda t: t[1], reverse=True)
    if not links:
        raise RuntimeError("no FAA cargo xlsx link found")
    href, cy = links[0]
    url = href if href.startswith("http") else "https://www.faa.gov" + href
    x = pd.read_excel(io.BytesIO(get(url).content), header=None)
    hdr_row = x.index[x.iloc[:, 0].astype(str).str.strip().eq("Rank")][0]
    df = x.iloc[hdr_row + 1:].copy()
    df.columns = [str(c).replace("\n", " ").strip() for c in x.iloc[hdr_row]]
    wcol = [c for c in df.columns if c.startswith(cy) and "Landed" in c][0]
    df = df[["Rank", "ST", "Locid", "Airport Name", "City", wcol]].rename(columns={wcol: "landed_lbs"})
    df = df[df["Locid"].notna()]
    ids = [str(i).strip() for i in df["Locid"]]
    coords = []
    for i in range(0, len(ids), 60):
        chunk = ",".join(f"'{a}'" for a in ids[i:i + 60])
        fc = arcgis(f"{NTAD}/NTAD_Aviation_Facilities/FeatureServer/0",
                    where=f"ARPT_ID IN ({chunk}) AND SITE_TYPE_CODE='A'",
                    fields="ARPT_ID,ARPT_NAME,LAT_DECIMAL,LONG_DECIMAL,STATE_CODE,CITY")
        coords.extend(f["properties"] for f in fc["features"])
    cdf = pd.DataFrame(coords).drop_duplicates("ARPT_ID")
    df["Locid"] = ids
    df = df.merge(cdf, left_on="Locid", right_on="ARPT_ID", how="left")
    save("faa/cargo_airports.csv", df.to_csv(index=False))
    record("air_cargo", url=url, vintage=f"CY{cy}",
           title=f"FAA all-cargo airports by landed weight, calendar year {cy}", n=int(df.LAT_DECIMAL.notna().sum()))


def src_tax_foundation():
    url = first_existing([f"https://taxfoundation.org/research/all/state/{y}-state-tax-competitiveness-index/"
                          for y in (YEAR + 1, YEAR, YEAR - 1)])
    if not url:
        raise RuntimeError("no Tax Foundation index page found")
    tables = pd.read_html(io.StringIO(get(url).text))
    t = next(t for t in tables if "Corporate Tax Rank" in [str(c) for c in t.columns])
    save("state/tax_foundation_index.csv", t.to_csv(index=False))
    y = re.search(r"/(\d{4})-state", url).group(1)
    record("tax_index", url=url, vintage=y, title=f"Tax Foundation {y} State Tax Competitiveness Index")

    curl = "https://taxfoundation.org/data/all/state/state-corporate-income-tax-rates-brackets/"
    html = get(curl).text
    title = re.search(r"<title>([^<]*)", html)
    ct = pd.read_html(io.StringIO(html))[0]
    save("state/corporate_rates.csv", ct.to_csv(index=False))
    record("corp_rates", url=curl, vintage=(re.search(r"(20\d\d)", title.group(1)).group(1) if title else ""),
           title="Tax Foundation state corporate income tax rates and brackets")


CNBC_URLS = [
    # Newest first. CNBC publishes each July; add the new year's "full rankings" URL here.
    "https://www.cnbc.com/2026/07/09/americas-top-states-for-business-full-rankings.html",
]


def src_cnbc():
    for url in CNBC_URLS:
        try:
            html = get(url).text
            t = pd.read_html(io.StringIO(html))[0]
            if len(t) >= 50:
                save("state/cnbc_top_states.csv", t.to_csv(index=False))
                record("cnbc", url=url, vintage=re.search(r"/(20\d\d)/", url).group(1),
                       title="CNBC America's Top States for Business (category ranks)")
                return
        except Exception:
            continue
    raise RuntimeError("no CNBC rankings table found")


def src_electricity():
    url = "https://www.eia.gov/electricity/monthly/epm_table_grapher.php?t=epmt_5_6_b"
    html = get(url).text
    tables = pd.read_html(io.StringIO(html))
    t = next(t for t in tables if t.shape[0] > 50)
    t.columns = [" | ".join(str(x) for x in c) if isinstance(c, tuple) else str(c) for c in t.columns]
    save("state/eia_prices_ytd.csv", t.to_csv(index=False))
    title = pd.read_html(io.StringIO(html))[0].to_string()
    m = re.search(r"Year-to-Date through (\w+ \d{4})", title)
    record("electricity", url=url, vintage=m.group(1) if m else "",
           title="EIA Electric Power Monthly Table 5.6.B: average price by state and sector, year to date")


def src_regulation():
    url = "https://quantgov-bulk-downloads.s3.amazonaws.com/State-RegData-5-0_Regulations.zip"
    tmp = Path("/tmp/state_regdata.zip")
    with get(url, stream=True, timeout=900) as r, open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    z = zipfile.ZipFile(tmp)
    totals = {}
    for name in sorted(n for n in z.namelist() if re.match(r"data_by_year/\d{4}_regulation_restrictions\.csv", n)):
        year = int(name.split("/")[1][:4])
        with z.open(name) as fh:
            df = pd.read_csv(fh, usecols=lambda c: c in ("jurisdiction_name", "restrictions", "words"))
        for j, g in df.groupby("jurisdiction_name"):
            totals[j] = {"year": year, "restrictions": int(g.restrictions.sum()),
                         "words": int(g.words.sum()) if "words" in g else None}
    out = pd.DataFrame([{"jurisdiction": k, **v} for k, v in totals.items()])
    save("state/regulation_restrictions.csv", out.to_csv(index=False))
    record("regulation", url=url, vintage=f"{out.year.min()}-{out.year.max()}",
           title="Mercatus Center QuantGov State RegData 5.0: regulatory restrictions in state administrative code")
    tmp.unlink(missing_ok=True)


SOURCES = {
    "county_shapes": src_county_shapes,
    "population": src_population,
    "centroids": src_centroids,
    "acs": src_acs_occupations,
    "cbsa": src_cbsa,
    "qcew": src_qcew,
    "oews": src_oews,
    "intermodal": src_intermodal,
    "rail_yards": src_rail_yards,
    "class1_rail": src_class1_rail,
    "interstates": src_interstates,
    "ports": src_ports,
    "docks": src_docks,
    "air_cargo": src_air_cargo,
    "tax": src_tax_foundation,
    "cnbc": src_cnbc,
    "electricity": src_electricity,
    "regulation": src_regulation,
}


def main(argv):
    names = argv or list(SOURCES)
    failures = []
    for name in names:
        t0 = time.time()
        try:
            SOURCES[name]()
            print(f"ok    {name:14s} {time.time() - t0:6.1f}s", flush=True)
        except Exception as e:
            failures.append(name)
            print(f"FAIL  {name:14s} {time.time() - t0:6.1f}s  {e}", flush=True)
            traceback.print_exc()
    if failures:
        print(f"\n{len(failures)} source(s) failed: {failures}. Cached copies (if any) are kept.")
    (CACHE / "fetch_log.json").write_text(json.dumps({"date": TODAY.isoformat(), "failed": failures}))


if __name__ == "__main__":
    main(sys.argv[1:])
