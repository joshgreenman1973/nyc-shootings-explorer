#!/usr/bin/env python3
"""
Cross-reference: shooting hotspots vs chronically-broken-streetlight blocks,
on a MATCHED time window and with a contemporaneous (same-year) test.

Why matched: geocoded 311 "street light out" data begins in 2010 (per-year 311
files exist back to 2004, but pre-2010 files carry no lat/lon — only ZIP/borough
— so they can't be binned to a block). And a light reported out at some random
time need not be out when a shooting occurred. So:
  * Both layers are restricted to 2010-2026 (2006-2009 shootings excluded).
  * A block-YEAR test asks whether shootings actually happen more in the years a
    block's light was reported out — i.e. the light was likely out around then.

Grid: lat/lon rounded to 3 decimals (~110 m, roughly a block).
Sources: 311 2010-2019 (76ig-c548) + 2020-present (erm2-nwe9); shootings points.
Output: data/streetlight_overlap.json
"""
import json, urllib.request, urllib.parse, os, sys, math, collections, statistics

DOMAIN = "https://data.cityofnewyork.us/resource"
SL_2010, SL_2020 = "76ig-c548", "erm2-nwe9"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUTAGE_DESCR = ("Street Light Out", "Multiple Street Lights Out")
Y0, Y1 = 2010, 2026   # matched window (311 streetlight data starts 2010)


def fetch_grid_year(dataset):
    """Server-side bin: outage complaints per (cell, year)."""
    out = collections.Counter()   # (la,lo,yr) -> n
    where = "(" + " OR ".join(f"descriptor='{d}'" for d in OUTAGE_DESCR) + ") AND latitude IS NOT NULL"
    offset, page = 0, 50000
    while True:
        params = {
            "$select": "round(latitude,3) as la,round(longitude,3) as lo,date_extract_y(created_date) as yr,count(*) as n",
            "$where": where, "$group": "la,lo,yr", "$order": "la,lo,yr",
            "$limit": page, "$offset": offset,
        }
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "sl-overlap/2.0"})
        with urllib.request.urlopen(req, timeout=180) as r:
            rows = json.load(r)
        for row in rows:
            try:
                la, lo = round(float(row["la"]), 3), round(float(row["lo"]), 3)
                yr, n = int(row["yr"]), int(row["n"])
            except (KeyError, TypeError, ValueError):
                continue
            if Y0 <= yr <= Y1 and 40.4 < la < 41.0 and -74.3 < lo < -73.6:
                out[(la, lo, yr)] += n
        if len(rows) < page:
            break
        offset += page
        print(f"  {dataset}: {offset}+ rows...", file=sys.stderr)
    return out


print("Fetching streetlight outages by block-year (2010-2026)...", file=sys.stderr)
oy = collections.Counter()
for ds in (SL_2010, SL_2020):
    oy.update(fetch_grid_year(ds))
# collapse to cell totals and cell->{year:count}
sl = collections.Counter()                       # cell -> total outages
sl_years = collections.defaultdict(dict)         # cell -> {year: count}
for (la, lo, yr), n in oy.items():
    sl[(la, lo)] += n
    sl_years[(la, lo)][yr] = sl_years[(la, lo)].get(yr, 0) + n
print(f"  outage cells: {len(sl)}  complaints: {sum(sl.values()):,}", file=sys.stderr)

# --- shootings 2010-2026 to the same block-year grid ---
pts = json.load(open(os.path.join(DATA, "shootings_points.json")))["points"]
sh = collections.Counter()                       # cell -> total shootings (2010-26)
sh_fatal = collections.Counter()
sh_years = collections.defaultdict(dict)         # cell -> {year: count}
for lat, lon, yr, bi, fatal in pts:
    if not (Y0 <= yr <= Y1):
        continue
    cell = (round(lat, 3), round(lon, 3))
    sh[cell] += 1
    sh_years[cell][yr] = sh_years[cell].get(yr, 0) + 1
    if fatal:
        sh_fatal[cell] += 1
print(f"  shooting cells (2010-26): {len(sh)}  shootings: {sum(sh.values()):,}", file=sys.stderr)

# ============ CONTEMPORANEOUS (block-year) TEST ============
# Universe: blocks that had >=1 shooting in 2010-26. For each such block and each
# year, was the light reported out that year? Compare shooting rates.
years = list(range(Y0, Y1 + 1))
sh_in_outage_yrs = n_outage_byrs = sh_in_clear_yrs = n_clear_byrs = 0
shootings_same_year_outage = 0
shootings_total = 0
for cell in sh:
    oyrs = sl_years.get(cell, {})
    for y in years:
        s = sh_years[cell].get(y, 0)
        had_outage = oyrs.get(y, 0) > 0
        if had_outage:
            sh_in_outage_yrs += s
            n_outage_byrs += 1
        else:
            sh_in_clear_yrs += s
            n_clear_byrs += 1
        shootings_total += s
        if s and had_outage:
            shootings_same_year_outage += s
rate_outage = sh_in_outage_yrs / n_outage_byrs if n_outage_byrs else 0
rate_clear = sh_in_clear_yrs / n_clear_byrs if n_clear_byrs else 0
share_same_year = 100 * shootings_same_year_outage / shootings_total if shootings_total else 0
# baseline: across shooting-block-years, what share had an outage that year?
baseline_byr_outage = 100 * n_outage_byrs / (n_outage_byrs + n_clear_byrs) if (n_outage_byrs + n_clear_byrs) else 0

# ============ BLOCK-LEVEL OVERLAP (matched window) ============
def pct_threshold(counter, q):
    vals = sorted(counter.values())
    return vals[min(len(vals) - 1, int(len(vals) * q))] if vals else 0

sh_hot_thr = max(5, pct_threshold(sh, 0.90))
sl_chronic_thr = pct_threshold(sl, 0.90)
shooting_cells = [c for c, n in sh.items() if n >= sh_hot_thr]
chronic_sl = {c for c, n in sl.items() if n >= sl_chronic_thr}


def pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a)); db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else None

corr = pearson([sh[c] for c in sh], [sl.get(c, 0) for c in sh])
overlap = []
for c in shooting_cells:
    overlap.append({"lat": c[0], "lon": c[1], "shootings": sh[c], "fatal": sh_fatal.get(c, 0),
                    "outages": sl.get(c, 0), "is_chronic_sl": c in chronic_sl})
overlap.sort(key=lambda r: (r["is_chronic_sl"], r["shootings"], r["outages"]), reverse=True)
n_both = sum(1 for r in overlap if r["is_chronic_sl"])

# labels + representative address for top overlap blocks
pdata = json.load(open(os.path.join(DATA, "shootings_agg.json")))
pname = {p["precinct"]: p["name"] for p in pdata["precinct"]}
gj = json.load(open(os.path.join(DATA, "precincts.geojson")))
precs = []
for f in gj["features"]:
    try:
        p = int(f["properties"]["precinct"])
    except (TypeError, ValueError):
        continue
    g = f["geometry"]
    parts = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    rings = [poly[0] for poly in parts]
    allpts = [q for r in rings for q in r]
    lons = [q[0] for q in allpts]; lats = [q[1] for q in allpts]
    precs.append((p, rings, (min(lons), min(lats), max(lons), max(lats))))


def pip(lat, lon, ring):
    inside = False; n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def precinct_of(lat, lon):
    for p, rings, (mnx, mny, mxx, mxy) in precs:
        if mnx <= lon <= mxx and mny <= lat <= mxy and any(pip(lat, lon, r) for r in rings):
            return p
    return None


top = [r for r in overlap if r["is_chronic_sl"]][:40]
for r in top:
    r["precinct"] = precinct_of(r["lat"], r["lon"])
    r["name"] = pname.get(r["precinct"], "")


def rep_address(lat, lon):
    d = 0.0006
    where = (f"descriptor='Street Light Out' AND latitude between {lat-d} and {lat+d} "
             f"AND longitude between {lon-d} and {lon+d} AND incident_address IS NOT NULL")
    url = f"{DOMAIN}/{SL_2020}.json?" + urllib.parse.urlencode({"$select": "incident_address", "$where": where, "$limit": 1})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sl/2.0"}), timeout=60) as rq:
            rows = json.load(rq)
        return rows[0].get("incident_address", "") if rows else ""
    except Exception:
        return ""


print("Fetching representative addresses...", file=sys.stderr)
for r in top[:15]:
    r["address"] = rep_address(r["lat"], r["lon"])

out = {
    "meta": {
        "window": [Y0, Y1], "grid": "~110 m block (lat/lon to 3 decimals)",
        "n_shooting_hotspot_cells": len(shooting_cells), "n_overlap": n_both,
        "overlap_share": round(100 * n_both / len(shooting_cells), 1) if shooting_cells else 0,
        "corr": round(corr, 3) if corr is not None else None,
        "total_outage_complaints": sum(sl.values()),
        "shootings_2010_26": sum(sh.values()),
        # contemporaneous block-year test
        "rate_outage_years": round(rate_outage, 3),
        "rate_clear_years": round(rate_clear, 3),
        "rate_ratio": round(rate_outage / rate_clear, 2) if rate_clear else None,
        "share_shootings_same_year_outage": round(share_same_year, 1),
        "baseline_blockyear_outage_share": round(baseline_byr_outage, 1),
        "sources": {"sl_2010_19": f"{DOMAIN}/{SL_2010}", "sl_2020_present": f"{DOMAIN}/{SL_2020}"},
    },
    "top_overlap": top,
    "scatter": [{"s": r["shootings"], "o": r["outages"], "c": 1 if r["is_chronic_sl"] else 0} for r in overlap],
    "chronic_cells": [[c[0], c[1], sl[c]] for c in chronic_sl],
}
with open(os.path.join(DATA, "streetlight_overlap.json"), "w") as f:
    json.dump(out, f, separators=(",", ":"))

print("\n=== MATCHED 2010-2026 ===", file=sys.stderr)
print(f"shooting-hotspot blocks (>= {sh_hot_thr}): {len(shooting_cells)}; also chronic-outage: {n_both} ({out['meta']['overlap_share']}%)", file=sys.stderr)
print(f"block correlation shootings~outages: {out['meta']['corr']}", file=sys.stderr)
print("\n=== CONTEMPORANEOUS (block-year) ===", file=sys.stderr)
print(f"shooting rate per block-year: outage-years {rate_outage:.3f} vs clear-years {rate_clear:.3f}  (ratio {out['meta']['rate_ratio']}x)", file=sys.stderr)
print(f"share of 2010-26 shootings whose block had a same-year outage complaint: {share_same_year:.1f}%  (baseline block-years w/ outage: {baseline_byr_outage:.1f}%)", file=sys.stderr)
