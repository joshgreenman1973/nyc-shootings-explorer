#!/usr/bin/env python3
"""
Cross-reference: chronic shooting-hotspot blocks vs chronic broken-streetlight blocks.

- Streetlight outages: NYC 311 "Street Light Out" / "Multiple Street Lights Out"
  complaints, 2010-2019 (76ig-c548) + 2020-present (erm2-nwe9), binned to a
  ~block grid (lat/lon rounded to 3 decimals, ~110m) via server-side aggregation.
- Shootings: this project's incident points (2006-2026), same grid.

Identifies grid cells that rank high on BOTH, labels them by precinct/neighborhood,
and reports how concentrated the overlap is. Output: data/streetlight_overlap.json.

Honest caveats (also surfaced on the page): both layers rise with population and,
for 311, with residents' propensity to report — so overlap is descriptive, not
causal, and partly reflects the same dense, lower-income geography.
"""
import json, urllib.request, urllib.parse, os, sys, math, collections

DOMAIN = "https://data.cityofnewyork.us/resource"
SL_2010 = "76ig-c548"   # 311 2010-2019
SL_2020 = "erm2-nwe9"   # 311 2020-present
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUTAGE_DESCR = ("Street Light Out", "Multiple Street Lights Out")


def fetch_grid(dataset):
    """Server-side bin of outage complaints to 3-decimal lat/lon cells."""
    grid = collections.Counter()
    where = "(" + " OR ".join(f"descriptor='{d}'" for d in OUTAGE_DESCR) + ") AND latitude IS NOT NULL"
    offset, page = 0, 50000
    while True:
        params = {
            "$select": "round(latitude,3) as la,round(longitude,3) as lo,count(*) as n",
            "$where": where, "$group": "la,lo", "$order": "la,lo",
            "$limit": page, "$offset": offset,
        }
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "sl-overlap/1.0"})
        with urllib.request.urlopen(req, timeout=180) as r:
            rows = json.load(r)
        for row in rows:
            try:
                la, lo, n = round(float(row["la"]), 3), round(float(row["lo"]), 3), int(row["n"])
            except (KeyError, TypeError, ValueError):
                continue
            if 40.4 < la < 41.0 and -74.3 < lo < -73.6:
                grid[(la, lo)] += n
        if len(rows) < page:
            break
        offset += page
        print(f"  {dataset}: {offset} cells fetched...", file=sys.stderr)
    return grid


print("Fetching streetlight-outage grids (2010-19 + 2020-present)...", file=sys.stderr)
sl = collections.Counter()
for ds in (SL_2010, SL_2020):
    sl.update(fetch_grid(ds))
print(f"  streetlight cells: {len(sl)}  total complaints: {sum(sl.values()):,}", file=sys.stderr)

# --- bin shootings to the same grid ---
pts = json.load(open(os.path.join(DATA, "shootings_points.json")))["points"]
sh = collections.Counter()
sh_fatal = collections.Counter()
for lat, lon, yr, bi, fatal in pts:
    cell = (round(lat, 3), round(lon, 3))
    sh[cell] += 1
    if fatal:
        sh_fatal[cell] += 1
print(f"  shooting cells: {len(sh)}", file=sys.stderr)

# --- precinct labels via point-in-polygon (reuse geojson) ---
PRECINCT_NAME = json.load(open(os.path.join(HERE, "data", "shootings_agg.json")))
pname = {p["precinct"]: p["name"] for p in PRECINCT_NAME["precinct"]}
gj = json.load(open(os.path.join(DATA, "precincts.geojson")))


def rings_of(f):
    g = f["geometry"]
    parts = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    return [poly[0] for poly in parts]


precs = []
for f in gj["features"]:
    try:
        p = int(f["properties"]["precinct"])
    except (TypeError, ValueError):
        continue
    rings = rings_of(f)
    allpts = [pt for r in rings for pt in r]
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


# --- rank cells; define "chronic" by percentile of cells that have any shooting ---
def pct_threshold(counter, q):
    vals = sorted(counter.values())
    if not vals:
        return 0
    return vals[min(len(vals) - 1, int(len(vals) * q))]

# shooting hotspots: cells in the top of the shooting distribution
sh_hot_thr = max(6, pct_threshold(sh, 0.90))        # >=6 shootings (top decile-ish)
sl_chronic_thr = pct_threshold(sl, 0.90)            # top decile of outage cells

shooting_cells = [c for c, n in sh.items() if n >= sh_hot_thr]
chronic_sl = {c for c, n in sl.items() if n >= sl_chronic_thr}

overlap = []
for c in shooting_cells:
    s_out = sl.get(c, 0)
    overlap.append({
        "lat": c[0], "lon": c[1], "shootings": sh[c], "fatal": sh_fatal.get(c, 0),
        "outages": s_out, "is_chronic_sl": c in chronic_sl,
    })
overlap.sort(key=lambda r: (r["is_chronic_sl"], r["shootings"], r["outages"]), reverse=True)

n_both = sum(1 for r in overlap if r["is_chronic_sl"])
# Pearson correlation across ALL shooting cells (not just hotspots): shootings vs outages
import statistics
xs = [sh[c] for c in sh]; ys = [sl.get(c, 0) for c in sh]


def pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else None

corr = pearson(xs, ys)

# label + representative address for the top overlap cells
top = [r for r in overlap if r["is_chronic_sl"]][:40]
for r in top:
    r["precinct"] = precinct_of(r["lat"], r["lon"])
    r["name"] = pname.get(r["precinct"], "")


def rep_address(lat, lon):
    """One representative cross-street/address for a cell, from 311."""
    lo0, lo1 = lon - 0.0006, lon + 0.0006
    la0, la1 = lat - 0.0006, lat + 0.0006
    where = (f"descriptor='Street Light Out' AND latitude between {la0} and {la1} "
             f"AND longitude between {lo0} and {lo1} AND incident_address IS NOT NULL")
    url = f"{DOMAIN}/{SL_2020}.json?" + urllib.parse.urlencode(
        {"$select": "incident_address,borough", "$where": where, "$limit": 1})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sl/1.0"}), timeout=60) as rq:
            rows = json.load(rq)
        if rows:
            return rows[0].get("incident_address", "")
    except Exception:
        pass
    return ""


print("Fetching representative addresses for top overlap blocks...", file=sys.stderr)
for r in top[:15]:
    r["address"] = rep_address(r["lat"], r["lon"])

# baseline: median outage count among ALL shooting cells vs citywide cells
sh_cells_out = [sl.get(c, 0) for c in sh]
out = {
    "meta": {
        "grid": "lat/lon rounded to 3 decimals (~110 m, roughly a block)",
        "shooting_hotspot_threshold": sh_hot_thr,
        "chronic_streetlight_threshold": sl_chronic_thr,
        "n_shooting_hotspot_cells": len(shooting_cells),
        "n_chronic_streetlight_cells": len(chronic_sl),
        "n_overlap": n_both,
        "overlap_share": round(100 * n_both / len(shooting_cells), 1) if shooting_cells else 0,
        "corr_shootings_outages_across_hotspots": round(corr, 3) if corr is not None else None,
        "median_outages_in_shooting_cells": int(statistics.median(sh_cells_out)) if sh_cells_out else 0,
        "median_outages_citywide": int(statistics.median(list(sl.values()))) if sl else 0,
        "total_outage_complaints": sum(sl.values()),
        "sources": {"sl_2010_19": f"{DOMAIN}/{SL_2010}", "sl_2020_present": f"{DOMAIN}/{SL_2020}"},
    },
    "top_overlap": top,
    # every shooting-hotspot block, for the scatter (shootings vs reported outages)
    "scatter": [{"s": r["shootings"], "o": r["outages"], "c": 1 if r["is_chronic_sl"] else 0}
                for r in overlap],
    # chronic streetlight-outage cells (top decile) for the map layer
    "chronic_cells": [[c[0], c[1], sl[c]] for c in chronic_sl],
}
with open(os.path.join(DATA, "streetlight_overlap.json"), "w") as f:
    json.dump(out, f, separators=(",", ":"))

print("\n=== RESULT ===", file=sys.stderr)
print(f"Shooting-hotspot blocks (>= {sh_hot_thr} shootings): {len(shooting_cells)}", file=sys.stderr)
print(f"Of those, also top-decile chronic streetlight-outage blocks: {n_both} ({out['meta']['overlap_share']}%)", file=sys.stderr)
print(f"Median outage complaints: shooting-hotspot blocks {out['meta']['median_outages_in_shooting_cells']} vs citywide cell {out['meta']['median_outages_citywide']}", file=sys.stderr)
print(f"Correlation (shootings vs outages across hotspot blocks): {out['meta']['corr_shootings_outages_across_hotspots']}", file=sys.stderr)
print("\nTop overlap blocks:", file=sys.stderr)
for r in top[:15]:
    print(f"  {r.get('name','?'):<18} pct {r.get('precinct')}  {r['shootings']} shootings / {r['outages']} outages  {r.get('address','')}", file=sys.stderr)
