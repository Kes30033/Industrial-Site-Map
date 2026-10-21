"""Turn the cached source data into the compact JSON files the map loads.

Inputs:  cache/   (written by fetch.py)
Outputs: site/data/metrics.json      per-county raw factor values (columnar)
         site/data/states.json       state business-climate figures
         site/data/areas.json        OEWS labor-market areas (metro / state nonmetro)
         site/data/points.json       intermodal terminals, rail yards, ports, cargo airports
         site/data/meta.json         source list, vintages, build date
         site/data/*.topo.json       county shapes, Class I rail, interstates (via mapshaper)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUT = ROOT / "site" / "data"
R_MI = 3958.8

STATES = {
    "01": ("AL", "Alabama"), "02": ("AK", "Alaska"), "04": ("AZ", "Arizona"), "05": ("AR", "Arkansas"),
    "06": ("CA", "California"), "08": ("CO", "Colorado"), "09": ("CT", "Connecticut"), "10": ("DE", "Delaware"),
    "11": ("DC", "District of Columbia"), "12": ("FL", "Florida"), "13": ("GA", "Georgia"), "15": ("HI", "Hawaii"),
    "16": ("ID", "Idaho"), "17": ("IL", "Illinois"), "18": ("IN", "Indiana"), "19": ("IA", "Iowa"),
    "20": ("KS", "Kansas"), "21": ("KY", "Kentucky"), "22": ("LA", "Louisiana"), "23": ("ME", "Maine"),
    "24": ("MD", "Maryland"), "25": ("MA", "Massachusetts"), "26": ("MI", "Michigan"), "27": ("MN", "Minnesota"),
    "28": ("MS", "Mississippi"), "29": ("MO", "Missouri"), "30": ("MT", "Montana"), "31": ("NE", "Nebraska"),
    "32": ("NV", "Nevada"), "33": ("NH", "New Hampshire"), "34": ("NJ", "New Jersey"), "35": ("NM", "New Mexico"),
    "36": ("NY", "New York"), "37": ("NC", "North Carolina"), "38": ("ND", "North Dakota"), "39": ("OH", "Ohio"),
    "40": ("OK", "Oklahoma"), "41": ("OR", "Oregon"), "42": ("PA", "Pennsylvania"), "44": ("RI", "Rhode Island"),
    "45": ("SC", "South Carolina"), "46": ("SD", "South Dakota"), "47": ("TN", "Tennessee"), "48": ("TX", "Texas"),
    "49": ("UT", "Utah"), "50": ("VT", "Vermont"), "51": ("VA", "Virginia"), "53": ("WA", "Washington"),
    "54": ("WV", "West Virginia"), "55": ("WI", "Wisconsin"), "56": ("WY", "Wyoming"),
}
NAME_TO_FIPS = {v[1].lower(): k for k, v in STATES.items()}
NAME_TO_FIPS.update({"washington, d.c.": "11", "washington dc": "11", "d.c.": "11", "dc": "11"})
ABBR_TO_FIPS = {v[0]: k for k, v in STATES.items()}

# Right-to-work states (NCSL / National Right to Work Foundation, checked October 2026).
RIGHT_TO_WORK = {"AL", "AZ", "AR", "FL", "GA", "ID", "IN", "IA", "KS", "KY", "LA", "MS", "NE", "NV", "NC",
                 "ND", "OK", "SC", "SD", "TN", "TX", "UT", "VA", "WV", "WI", "WY"}
# States with no corporate income tax but a gross receipts tax instead.
GROSS_RECEIPTS_INSTEAD = {"NV": "Commerce Tax (gross receipts)", "OH": "Commercial Activity Tax (gross receipts)",
                          "TX": "Franchise (margin) tax", "WA": "Business & Occupation tax (gross receipts)"}

CLASS1_NAMES = {"BNSF": "BNSF Railway", "UP": "Union Pacific", "CSXT": "CSX", "NS": "Norfolk Southern",
                "CN": "Canadian National", "CPRS": "CPKC", "CPKC": "CPKC", "KCS": "CPKC (Kansas City Southern)",
                "CP": "CPKC", "SOO": "CPKC (Soo Line)", "IC": "CN (Illinois Central)", "GTW": "CN (Grand Trunk)",
                "WC": "CN (Wisconsin Central)", "DMIR": "CN", "BLE": "CN", "EJE": "CN", "DWP": "CN",
                "KCSM": "CPKC", "DME": "CPKC", "ICE": "CPKC", "AMTK": "Amtrak", "FEC": "Florida East Coast"}

CLASS1_OWNERS = {"BNSF", "UP", "CSXT", "NS", "CN", "CPRS", "CPKC", "KCS", "CP", "SOO", "IC", "GTW", "WC", "KCSM"}

LABOR_RADIUS_MI = 45.0   # typical commuting shed
MARKET_RADIUS_MI = 250.0  # same-day / next-morning truck delivery range
PORT_MIN_TONS = 5_000_000  # "major port" cutoff (short tons per year)
AIR_MIN_LBS = 250e6  # "cargo airport" cutoff (all-cargo landed weight, lbs per year)


# ----------------------------------------------------------------------------- geometry helpers
def xyz(lat, lon):
    lat = np.radians(np.asarray(lat, float))
    lon = np.radians(np.asarray(lon, float))
    return np.column_stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])


def chord_to_mi(c):
    return 2 * R_MI * np.arcsin(np.clip(np.asarray(c) / 2, 0, 1))


def mi_to_chord(mi):
    return 2 * math.sin(mi / (2 * R_MI))


def iter_lines(geom):
    if geom is None:
        return
    t = geom["type"]
    if t == "LineString":
        yield geom["coordinates"]
    elif t == "MultiLineString":
        yield from geom["coordinates"]
    elif t == "Polygon":
        yield from geom["coordinates"]
    elif t == "MultiPolygon":
        for poly in geom["coordinates"]:
            yield from poly


def densify(coords, step_mi=0.4):
    """Points along a polyline no more than ~step_mi apart (lon/lat input, returns lat, lon arrays)."""
    pts = np.asarray(coords, float)
    if len(pts) == 0:
        return np.empty(0), np.empty(0)
    lats, lons = [pts[0, 1]], [pts[0, 0]]
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        dy = (y1 - y0) * 69.0
        dx = (x1 - x0) * 69.0 * math.cos(math.radians((y0 + y1) / 2))
        n = int(math.hypot(dx, dy) / step_mi)
        for k in range(1, n + 1):
            f = k / (n + 1)
            lats.append(y0 + f * (y1 - y0))
            lons.append(x0 + f * (x1 - x0))
        lats.append(y1)
        lons.append(x1)
    return np.array(lats), np.array(lons)


def line_points(fc, step_mi=0.4, keep=None):
    """Densified points for every feature in a FeatureCollection, plus the feature index of each point."""
    la, lo, ix = [], [], []
    for i, f in enumerate(fc["features"]):
        if keep is not None and not keep(f["properties"]):
            continue
        for line in iter_lines(f.get("geometry")):
            a, b = densify(line, step_mi)
            la.append(a)
            lo.append(b)
            ix.append(np.full(len(a), i))
    return np.concatenate(la), np.concatenate(lo), np.concatenate(ix)


def nearest(c_lat, c_lon, p_lat, p_lon):
    tree = cKDTree(xyz(p_lat, p_lon))
    d, i = tree.query(xyz(c_lat, c_lon))
    return chord_to_mi(d), i


def num(v):
    try:
        if v is None:
            return None
        s = str(v).replace(",", "").replace("$", "").replace("%", "").strip()
        if s in ("", "-", "*", "#", "nan", "NaN", "None", "(8)", "(5)", "(4)"):
            return None
        return float(s)
    except ValueError:
        return None


def load_json(rel):
    return json.loads((CACHE / rel).read_text())


# ----------------------------------------------------------------------------- counties
def load_counties() -> pd.DataFrame:
    pop = pd.read_csv(CACHE / "census/population.csv", dtype=str)
    pop["fips"] = pop.STATE + pop.COUNTY
    pop = pop[pop.STATE.isin(STATES)]
    df = pd.DataFrame({
        "fips": pop.fips, "st_fips": pop.STATE,
        "name": pop.CTYNAME.str.strip(), "pop": pop.POP.astype(float),
    }).set_index("fips")

    cen = pd.read_csv(CACHE / "census/cenpop2020_county.csv", dtype=str)
    cen.columns = [c.strip().lstrip("﻿") for c in cen.columns]
    cen["fips"] = cen.STATEFP + cen.COUNTYFP
    cen = cen.set_index("fips")
    gaz_path = CACHE / "census/gazetteer_counties.txt"
    first = gaz_path.open(encoding="utf-8").readline()
    gaz = pd.read_csv(gaz_path, sep="|" if "|" in first else "\t", dtype=str)  # 2026+ files are pipe-delimited
    gaz.columns = [c.strip() for c in gaz.columns]
    gaz = gaz.set_index("GEOID")

    lat, lon, area = [], [], []
    for f in df.index:
        if f in cen.index:  # population-weighted centre (better for big western counties)
            lat.append(float(cen.at[f, "LATITUDE"]))
            lon.append(float(cen.at[f, "LONGITUDE"]))
        elif f in gaz.index:  # e.g. Connecticut planning regions (created 2022)
            lat.append(float(gaz.at[f, "INTPTLAT"]))
            lon.append(float(gaz.at[f, "INTPTLONG"]))
        else:
            lat.append(np.nan)
            lon.append(np.nan)
        area.append(float(gaz.at[f, "ALAND_SQMI"]) if f in gaz.index else np.nan)
    df["lat"], df["lon"], df["land_sqmi"] = lat, lon, area
    missing = df[df.lat.isna()]
    if len(missing):
        print("WARNING: counties without a centroid:", list(missing.index))
    df = df[df.lat.notna()]
    df["st"] = df.st_fips.map(lambda s: STATES[s][0])
    print(f"counties: {len(df)}")
    return df


# ----------------------------------------------------------------------------- logistics
def logistics_ports(df: pd.DataFrame, points: dict) -> None:
    """Ports handled separately so indices stay consistent."""
    fc = load_json("ntad/principal_ports.geojson")
    ports, bl, bo, bi = [], [], [], []
    for f in fc["features"]:
        p = f["properties"]
        tons = num(p.get("total")) or 0
        coords = [c for line in iter_lines(f.get("geometry")) for c in line]
        if not coords:
            continue
        a = np.asarray(coords, float)
        ports.append({"lat": round(a[:, 1].mean(), 4), "lon": round(a[:, 0].mean(), 4),
                      "name": clean_port(p.get("portname") or ""), "tons": int(tons),
                      "type": p.get("type") or "", "geom": f["geometry"]})
    ports.sort(key=lambda r: -r["tons"])
    for k, pt in enumerate(ports):
        if pt["tons"] >= PORT_MIN_TONS:
            for line in iter_lines(pt["geom"]):
                la_, lo_ = densify(line, 1.0)
                bl.append(la_)
                bo.append(lo_)
                bi.append(np.full(len(la_), k))
    d, i = nearest(df.lat.values, df.lon.values, np.concatenate(bl), np.concatenate(bo))
    df["d_port"] = d
    df["port_i"] = np.concatenate(bi)[i]
    points["ports"] = [[p["lat"], p["lon"], p["name"], p["tons"], p["type"]] for p in ports]
    print(f"ports: {len(ports)} principal ports, {sum(1 for p in ports if p['tons'] >= PORT_MIN_TONS)} with {PORT_MIN_TONS/1e6:.0f}M+ tons")


def logistics_air(df: pd.DataFrame, points: dict, min_lbs=AIR_MIN_LBS) -> None:
    a = pd.read_csv(CACHE / "faa/cargo_airports.csv")
    a = a[a.LAT_DECIMAL.notna()].copy()
    a["landed_lbs"] = a.landed_lbs.map(num)
    a = a.sort_values("landed_lbs", ascending=False)
    rows = [[round(r.LAT_DECIMAL, 4), round(r.LONG_DECIMAL, 4), r.Locid, title_case(str(r["Airport Name"])),
             int(r.landed_lbs or 0), f"{title_case(str(r.City))}, {r.ST}"] for _, r in a.iterrows()]
    big = [k for k, r in enumerate(rows) if r[4] >= min_lbs]
    d, i = nearest(df.lat.values, df.lon.values, np.array([rows[k][0] for k in big]),
                   np.array([rows[k][1] for k in big]))
    df["d_air"] = d
    df["air_i"] = np.array(big)[i]
    points["airports"] = rows
    print(f"cargo airports: {len(rows)}, {len(big)} with >= {min_lbs / 1e6:.0f}M lbs landed weight")


def market_and_sheds(df: pd.DataFrame) -> None:
    pts = xyz(df.lat.values, df.lon.values)
    tree = cKDTree(pts)
    pop = df["pop"].values
    near = tree.query_ball_point(pts, mi_to_chord(MARKET_RADIUS_MI))
    df["market_pop"] = [pop[n].sum() for n in near]
    shed = tree.query_ball_point(pts, mi_to_chord(LABOR_RADIUS_MI))
    for col in [c for c in df.columns if c.startswith("w_")]:
        v = df[col].fillna(0).values
        df["shed_" + col[2:]] = [v[n].sum() for n in shed]
    df["shed_counties"] = [len(n) for n in shed]


# ----------------------------------------------------------------------------- labor
def labor(df: pd.DataFrame) -> None:
    # ACS occupation groups (where workers live)
    acs = pd.read_csv(CACHE / "census/acs_c24010_county.psv", sep="|", dtype=str)
    acs["fips"] = acs.GEO_ID.str[-5:]
    acs = acs.set_index("fips")

    def col(n):
        return acs[f"C24010_E{n:03d}"].map(num)

    acs_tot = col(1)
    trades = col(32) + col(33) + col(35) + col(68) + col(69) + col(71)   # construction, maintenance/repair, production
    logi = col(36) + col(37) + col(72) + col(73)                         # transportation, material moving
    prod = col(35) + col(71)
    df["w_trades"] = trades.reindex(df.index)
    df["w_logi"] = logi.reindex(df.index)
    df["w_prod"] = prod.reindex(df.index)
    df["acs_workers"] = acs_tot.reindex(df.index)

    # QCEW industry employment (where jobs are)
    q = pd.read_csv(CACHE / "bls/qcew_county.csv", dtype=str)
    q = q[q.area_fips.str.fullmatch(r"\d{5}") & ~q.area_fips.str.endswith("999")]
    q["emp"] = q.annual_avg_emplvl.map(num).fillna(0)
    q["lq"] = q.lq_annual_avg_emplvl.map(num)
    piv = q.pivot_table(index="area_fips", columns="industry_code", values="emp", aggfunc="sum").reindex(df.index)
    lq = q.pivot_table(index="area_fips", columns="industry_code", values="lq", aggfunc="first").reindex(df.index)

    def g(code):
        return piv[code] if code in piv else pd.Series(0, index=df.index)

    df["w_metal"] = g("332").fillna(0) + g("333").fillna(0) + g("336").fillna(0)
    df["w_whse"] = g("493").fillna(0) + g("484").fillna(0)
    df["w_mfg"] = g("31-33").fillna(0) if "31-33" in piv else g("31_33").fillna(0)
    df["jobs_total"] = g("10")
    df["mfg_lq"] = lq["31-33"] if "31-33" in lq else (lq["31_33"] if "31_33" in lq else np.nan)


def oews_areas(df: pd.DataFrame) -> dict:
    """Assign each county to its OEWS labor market (metro area, or the state's nonmetro balance)."""
    o = load_json("bls/oews.json")
    rows = pd.DataFrame(o["rows"])
    rows["v"] = rows.value.map(num)
    occs = list(o["occupations"])

    cbsa = pd.read_csv(CACHE / "census/cbsa_2023.csv", dtype=str)
    cbsa = cbsa[cbsa["Metropolitan/Micropolitan Statistical Area"] == "Metropolitan Statistical Area"]
    cbsa["fips"] = cbsa["FIPS State Code"].str.zfill(2) + cbsa["FIPS County Code"].str.zfill(3)
    county_cbsa = dict(zip(cbsa.fips, cbsa["CBSA Code"]))
    title_cbsa = dict(zip(cbsa["CBSA Title"], cbsa["CBSA Code"]))

    m = rows[rows.area_type == "M"].copy()
    m["code"] = m.area_code
    no_code = m.code.isna()
    m.loc[no_code, "code"] = m.loc[no_code, "area_name"].map(lambda n: ("00" + title_cbsa[n]) if n in title_cbsa else None)
    m = m[m.code.notna()]
    s = rows[rows.area_type == "S"].copy()
    s["code"] = "S" + s.state.str.zfill(2)

    def table(frame):
        t = {}
        for (code, occ, meas), v in frame.set_index(["code", "occ", "measure"]).v.items():
            t.setdefault(code, {}).setdefault(occ, {})[meas] = None if pd.isna(v) else float(v)
        return t

    mt, st = table(m), table(s)
    names = dict(zip(m.code, m.area_name))
    names.update({k: STATES[k[1:]][1] if k[1:] in STATES else k for k in st})

    areas = {}
    # metro areas
    for code, occd in mt.items():
        if code.startswith("00"):
            areas[code] = {"name": names[code], "type": "metro", "occ": occd}
    # state nonmetro balance: combine the state's nonmetro areas
    for stf in STATES:
        parts = [c for c in mt if not c.startswith("00") and c[:2] == stf]
        if not parts:
            continue
        comb = {}
        for occ in occs:
            emp = sum((mt[c].get(occ, {}).get("01") or 0) for c in parts)
            denom = sum((mt[c].get(occ, {}).get("01") or 0) / mt[c][occ]["17"]
                        for c in parts if mt[c].get(occ, {}).get("17") and mt[c][occ].get("01"))
            wsum = sum((mt[c].get(occ, {}).get("01") or 0) * (mt[c].get(occ, {}).get("13") or 0)
                       for c in parts if mt[c].get(occ, {}).get("13"))
            wemp = sum((mt[c].get(occ, {}).get("01") or 0) for c in parts if mt[c].get(occ, {}).get("13"))
            comb[occ] = {"01": emp or None, "17": (emp / denom) if denom else None,
                         "13": (wsum / wemp) if wemp else None}
        areas["N" + stf] = {"name": f"{STATES[stf][1]} (nonmetro areas)", "type": "nonmetro", "occ": comb}
    for code, occd in st.items():
        areas[code] = {"name": names[code], "type": "state", "occ": occd}

    # county -> area
    assign = []
    for f, stf in zip(df.index, df.st_fips):
        c = county_cbsa.get(f)
        if c and ("00" + c) in areas:
            assign.append("00" + c)
        elif ("N" + stf) in areas:
            assign.append("N" + stf)
        else:
            assign.append("S" + stf)
    df["area"] = assign

    # fill missing values from the state, then compute factor inputs
    def val(area, stf, occ, meas):
        v = areas.get(area, {}).get("occ", {}).get(occ, {}).get(meas)
        if v is None:
            v = areas.get("S" + stf, {}).get("occ", {}).get(occ, {}).get(meas)
        return v

    metal_occ = ["514121", "514041", "519161"]
    elec_occ = ["472111", "499041"]
    logi_occ = ["533032", "537062"]
    trade_wage_occ = metal_occ + elec_occ
    # national reference wage per occupation: employment-weighted mean of state medians
    ref = {}
    for occ in occs:
        num_, den = 0.0, 0.0
        for stf in STATES:
            e, w = st.get("S" + stf, {}).get(occ, {}).get("01"), st.get("S" + stf, {}).get(occ, {}).get("13")
            if e and w:
                num_ += e * w
                den += e
        ref[occ] = num_ / den if den else None

    def mean_of(vals):
        vals = [v for v in vals if v is not None]
        return float(np.mean(vals)) if vals else None

    df["lq_metal"] = [mean_of([val(a, s_, oc, "17") for oc in metal_occ]) for a, s_ in zip(df.area, df.st_fips)]
    df["lq_elec"] = [mean_of([val(a, s_, oc, "17") for oc in elec_occ]) for a, s_ in zip(df.area, df.st_fips)]
    df["lq_logi"] = [mean_of([val(a, s_, oc, "17") for oc in logi_occ]) for a, s_ in zip(df.area, df.st_fips)]
    df["wage_trade"] = [mean_of([(val(a, s_, oc, "13") / ref[oc]) if val(a, s_, oc, "13") and ref[oc] else None
                                 for oc in trade_wage_occ]) for a, s_ in zip(df.area, df.st_fips)]
    df["wage_logi"] = [mean_of([(val(a, s_, oc, "13") / ref[oc]) if val(a, s_, oc, "13") and ref[oc] else None
                                for oc in logi_occ]) for a, s_ in zip(df.area, df.st_fips)]

    used = set(df.area)
    out = {"release": o["release"], "occupations": o["occupations"], "ref_wage": ref, "areas": {}}
    for code in sorted(used | {k for k in areas if k.startswith("S")}):
        a = areas[code]
        out["areas"][code] = {
            "name": a["name"], "type": a["type"],
            "occ": {occ: [rnd(a["occ"].get(occ, {}).get("01"), 0), rnd(a["occ"].get(occ, {}).get("17"), 2),
                          rnd(a["occ"].get(occ, {}).get("13"), 0)] for occ in occs},
        }
    print(f"OEWS: {sum(1 for a in df.area if a.startswith('00'))} counties in metro areas, "
          f"{sum(1 for a in df.area if a.startswith('N'))} nonmetro, {sum(1 for a in df.area if a.startswith('S'))} state fallback")
    return out


# ----------------------------------------------------------------------------- states
def states_table() -> dict:
    out = {f: {"abbr": a, "name": n, "rtw": a in RIGHT_TO_WORK} for f, (a, n) in STATES.items()}

    def fips_for(name):
        n = re.sub(r"\s*\(.*?\)\s*", "", str(name)).strip().lower()
        return NAME_TO_FIPS.get(n)

    tf = pd.read_csv(CACHE / "state/tax_foundation_index.csv")
    for _, r in tf.iterrows():
        f = fips_for(r["State"])
        if not f:
            continue
        out[f].update({
            "tf_overall": num(re.sub(r"[()]", "", str(r.get("Overall Rank")))),
            "tf_corp": num(re.sub(r"[()]", "", str(r.get("Corporate Tax Rank")))),
            "tf_indiv": num(re.sub(r"[()]", "", str(r.get("Individual Income Tax Rank")))),
            "tf_sales": num(re.sub(r"[()]", "", str(r.get("Sales Tax Rank")))),
            "tf_prop": num(re.sub(r"[()]", "", str(r.get("Property Tax Rank")))),
            "tf_ui": num(re.sub(r"[()]", "", str(r.get("Unemployment Insurance Tax Rank")))),
        })

    ct = pd.read_csv(CACHE / "state/corporate_rates.csv")
    ct["State"] = ct["State"].ffill()
    ct["rate"] = ct[ct.columns[1]].astype(str).str.extract(r"([\d.]+)\s*%")[0].astype(float)
    for name, g in ct.groupby("State"):
        f = fips_for(name)
        if not f:
            continue
        top = g.rate.max()
        a = STATES[f][0]
        out[f]["corp_rate"] = None if pd.isna(top) or top == 0 else round(float(top), 2)
        if a in GROSS_RECEIPTS_INSTEAD:
            out[f]["corp_note"] = "No corporate income tax; " + GROSS_RECEIPTS_INSTEAD[a]
        elif out[f]["corp_rate"] is None:
            out[f]["corp_note"] = "No corporate income tax"

    cn = pd.read_csv(CACHE / "state/cnbc_top_states.csv")
    cols = {c: re.sub(r"[^a-z]", "", c.lower()) for c in cn.columns}
    cn = cn.rename(columns=cols)
    for _, r in cn.iterrows():
        f = fips_for(r["state"])
        if not f:
            continue
        out[f].update({
            "cnbc_overall": num(r.get("overall")),
            "cnbc_bf": num(r.get("businessfriendliness")),
            "cnbc_cost": num(r.get("costofdoingbusiness")),
            "cnbc_infra": num(r.get("infrastructure")),
            "cnbc_workforce": num(r.get("workforce")),
        })

    e = pd.read_csv(CACHE / "state/eia_prices_ytd.csv")
    name_col = e.columns[0]
    ind_col = next(c for c in e.columns if c.lower().startswith("industrial"))
    for _, r in e.iterrows():
        f = fips_for(r[name_col])
        if f:
            out[f]["elec"] = num(r[ind_col])

    rg = pd.read_csv(CACHE / "state/regulation_restrictions.csv")
    for _, r in rg.iterrows():
        f = fips_for(str(r.jurisdiction).replace("_", " "))
        if f:
            out[f]["restrictions"] = int(r.restrictions)
            out[f]["restr_year"] = int(r.year)

    missing = {k: [s["abbr"] for s in out.values() if s.get(k) is None]
               for k in ["tf_overall", "cnbc_bf", "elec", "restrictions"]}
    print("state gaps:", {k: v for k, v in missing.items() if v})
    return {s["abbr"]: {k: v for k, v in s.items() if k != "abbr"} for s in out.values()}


# ----------------------------------------------------------------------------- shapes
def topojson() -> None:
    ms = shutil.which("mapshaper")
    if not ms:
        print("mapshaper not installed; skipping topojson (existing files kept)")
        return
    tmp = Path("/tmp/shapes")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    zipfile.ZipFile(CACHE / "census/county_5m.zip").extractall(tmp)
    shp = next(tmp.glob("*.shp"))
    subprocess.run([ms, "-i", str(shp), "name=counties",
                    "-filter", "Number(STATEFP) <= 56",
                    "-simplify", "8%", "weighted", "keep-shapes",
                    "-each", "id=GEOID",
                    "-filter-fields", "id,STATEFP",
                    "-dissolve", "STATEFP", "+", "name=states",
                    "-filter-fields", "id", "target=counties",
                    "-o", str(OUT / "counties.topo.json"), "format=topojson", "quantization=100000",
                    "id-field=id", "target=*"], check=True)

    for src, out, key, names in [("ntad/class1_rail.geojson", "rail.topo.json", "RROWNER1", CLASS1_NAMES),
                                 ("ntad/interstates.geojson", "interstates.topo.json", None, None)]:
        fc = load_json(src)
        groups = {}
        for f in fc["features"]:
            k = (f["properties"].get(key) or "Other") if key else "Interstate"
            if names:
                k = names.get(k, k).split(" (")[0]
            groups.setdefault(k, []).extend(list(iter_lines(f.get("geometry"))))
        merged = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": k}, "geometry": {"type": "MultiLineString", "coordinates": v}}
            for k, v in groups.items()]}
        tmpf = tmp / (out + ".geojson")
        tmpf.write_text(json.dumps(merged))
        subprocess.run([ms, "-i", str(tmpf), "-simplify", "interval=400", "-clean",
                        "-o", str(OUT / out), "format=topojson", "quantization=100000"], check=True)
    for f in OUT.glob("*.topo.json"):
        print(f"{f.name}: {f.stat().st_size / 1e6:.2f} MB")


# ----------------------------------------------------------------------------- misc helpers
def rnd(v, n=1):
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        return round(float(v), n) if n else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def title_case(s: str) -> str:
    s = (s or "").strip()
    if not s or not s.isupper():
        return s
    small = {"OF", "AND", "THE", "AT", "ON"}
    words = []
    for w in s.split():
        if w in {"BNSF", "CSX", "UP", "NS", "CN", "CPKC", "KCS", "FEC", "LLC", "II", "III", "IAIS", "SP",
                 "ICTF", "TOFC", "COFC", "US", "USA"} or (len(w) <= 3 and not w.isalpha()):
            words.append(w)
        elif w in small and words:
            words.append(w.lower())
        else:
            words.append(w.capitalize())
    return " ".join(words)


def clean_port(n: str) -> str:
    n = re.sub(r"\s+", " ", n).strip()
    return n


def fmt_interstate(s: str) -> str:
    s = (s or "").strip().upper()
    m = re.match(r"^I-?\s*(\d+[A-Z]?)", s)
    return f"I-{m.group(1)}" if m else s


# ----------------------------------------------------------------------------- main
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    df = load_counties()
    points = {}
    logistics_core(df, points)
    logistics_ports(df, points)
    logistics_air(df, points)
    labor(df)
    market_and_sheds(df)
    areas = oews_areas(df)
    states = states_table()

    cols = {
        "fips": list(df.index), "name": list(df.name), "st": list(df.st),
        "pop": [rnd(v, 0) for v in df["pop"]], "lat": [rnd(v, 3) for v in df.lat], "lon": [rnd(v, 3) for v in df.lon],
        "d_interstate": [rnd(v) for v in df.d_interstate], "interstate": list(df.interstate),
        "d_class1": [rnd(v) for v in df.d_class1], "class1": list(df.class1), "class1_own": [int(v) for v in df.class1_own],
        "d_yard": [rnd(v) for v in df.d_yard], "yard_i": [int(v) for v in df.yard_i],
        "d_intermodal": [rnd(v) for v in df.d_intermodal], "intermodal_i": [int(v) for v in df.intermodal_i],
        "d_port": [rnd(v) for v in df.d_port], "port_i": [int(v) for v in df.port_i],
        "d_air": [rnd(v) for v in df.d_air], "air_i": [int(v) for v in df.air_i],
        "market_pop": [rnd(v, 0) for v in df.market_pop],
        "shed_trades": [rnd(v, 0) for v in df.shed_trades], "shed_logi": [rnd(v, 0) for v in df.shed_logi],
        "shed_metal": [rnd(v, 0) for v in df.shed_metal], "shed_whse": [rnd(v, 0) for v in df.shed_whse],
        "shed_mfg": [rnd(v, 0) for v in df.shed_mfg], "shed_prod": [rnd(v, 0) for v in df.shed_prod],
        "w_trades": [rnd(v, 0) for v in df.w_trades], "w_logi": [rnd(v, 0) for v in df.w_logi],
        "w_metal": [rnd(v, 0) for v in df.w_metal], "w_whse": [rnd(v, 0) for v in df.w_whse],
        "w_mfg": [rnd(v, 0) for v in df.w_mfg], "jobs_total": [rnd(v, 0) for v in df.jobs_total],
        "area": list(df.area),
        "lq_metal": [rnd(v, 2) for v in df.lq_metal], "lq_elec": [rnd(v, 2) for v in df.lq_elec],
        "lq_logi": [rnd(v, 2) for v in df.lq_logi],
        "wage_trade": [rnd(v, 3) for v in df.wage_trade], "wage_logi": [rnd(v, 3) for v in df.wage_logi],
    }
    write("metrics.json", cols)
    write("points.json", points)
    write("states.json", states)
    write("areas.json", areas)
    manifest = json.loads((CACHE / "manifest.json").read_text())
    write("meta.json", {"built": dt.date.today().isoformat(), "sources": manifest,
                        "labor_radius_mi": LABOR_RADIUS_MI, "market_radius_mi": MARKET_RADIUS_MI})
    topojson()
    sanity(df)


def logistics_core(df, points):
    """Interstates, Class I rail, rail yards, intermodal terminals."""
    clat, clon = df.lat.values, df.lon.values

    fc = load_json("ntad/interstates.geojson")
    la, lo, ix = line_points(fc, 0.4)
    d, i = nearest(clat, clon, la, lo)
    df["d_interstate"] = d
    df["interstate"] = [fmt_interstate(fc["features"][k]["properties"].get("SIGN1") or "") for k in ix[i]]

    yfc = load_json("ntad/rail_yards.geojson")
    owner_names = {}
    for f in yfc["features"]:
        p = f["properties"]
        if p.get("RROWNER1") and p.get("RROWNER1_NAME"):
            owner_names[p["RROWNER1"]] = title_case(p["RROWNER1_NAME"])

    fc = load_json("ntad/class1_rail.geojson")
    nets = pd.Series([f["properties"].get("NET") for f in fc["features"]]).value_counts()
    print("Class I NET codes:", nets.to_dict())
    la, lo, ix = line_points(fc, 0.4)
    d, i = nearest(clat, clon, la, lo)
    df["d_class1"] = d
    codes = [fc["features"][k]["properties"].get("RROWNER1") or "" for k in ix[i]]
    df["class1"] = [CLASS1_NAMES.get(c) or owner_names.get(c) or c for c in codes]
    df["class1_own"] = [1 if c in CLASS1_OWNERS else 0 for c in codes]

    fc = yfc
    groups = {}
    for f in fc["features"]:
        p = f["properties"]
        key = ((p.get("YARDNAME") or "").strip().upper(), p.get("RROWNER1") or "")
        for line in iter_lines(f.get("geometry")):
            groups.setdefault(key, []).extend(line)
    yards = []
    for (name, owner), coords in groups.items():
        a = np.asarray(coords, float)
        yards.append([round(a[:, 1].mean(), 4), round(a[:, 0].mean(), 4), title_case(name) or "Unnamed yard", owner])
    d, i = nearest(clat, clon, np.array([y[0] for y in yards]), np.array([y[1] for y in yards]))
    df["d_yard"], df["yard_i"] = d, i
    points["yards"] = yards
    points["railroads"] = owner_names
    print(f"rail yards: {len(yards)} named yards")

    fc = load_json("ntad/intermodal_rail.geojson")
    terms = []
    for f in fc["features"]:
        p = f["properties"]
        lat_, lon_ = p.get("LAT"), p.get("LON")
        if (lat_ is None or lon_ is None) and f.get("geometry"):
            lon_, lat_ = f["geometry"]["coordinates"][:2]
        if lat_ is None:
            continue
        name = (p.get("TERMINAL") or "").strip()
        port = (p.get("PORT") or "").strip()
        if port and (not name or name.lower() in ("seaport", "port", "port terminal")):
            name = port
        elif port and port.lower() not in name.lower():
            name = f"{name} ({port})"
        terms.append([round(float(lat_), 4), round(float(lon_), 4), name or "Intermodal terminal",
                      (p.get("RAIL_CO") or "").strip(), f"{title_case(p.get('CITY') or '')}, {p.get('STATE') or ''}"])
    d, i = nearest(clat, clon, np.array([t[0] for t in terms]), np.array([t[1] for t in terms]))
    df["d_intermodal"], df["intermodal_i"] = d, i
    points["intermodal"] = terms
    print(f"intermodal terminals: {len(terms)}")


def write(name, obj):
    p = OUT / name
    p.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False, default=lambda o: None))
    print(f"{name}: {p.stat().st_size / 1e3:.0f} KB")


def sanity(df):
    """Print a few well-known places so a run log shows whether the numbers make sense."""
    checks = {"17031": "Cook IL (Chicago)", "48201": "Harris TX (Houston)", "47157": "Shelby TN (Memphis)",
              "06037": "Los Angeles CA", "39035": "Cuyahoga OH (Cleveland)", "30001": "Beaverhead MT (rural)"}
    cols = ["d_interstate", "d_class1", "d_yard", "d_intermodal", "d_port", "d_air", "market_pop",
            "shed_trades", "shed_metal", "lq_metal", "wage_trade", "area"]
    print(df.loc[[f for f in checks if f in df.index], cols].round(2).to_string())


if __name__ == "__main__":
    main()
