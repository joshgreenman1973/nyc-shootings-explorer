#!/usr/bin/env python3
"""
Actual precinct populations over time, from the 2010 and 2020 decennial censuses.

Method: fetch total population by census tract (2010 SF1 P001001, 2020 PL P1_001N)
for the five NYC counties, locate each tract's internal point from the Census
gazetteer, and assign it (point-in-polygon) to a police precinct (stable
boundaries). Sum tract populations per precinct for each census, then interpolate
linearly between 2010 and 2020 and extrapolate outside that window.

This captures real precinct-level population change (e.g., booming Downtown
Brooklyn / Long Island City) that a borough-wide scaling cannot.

Output: data/precinct_pop_years.json  -> {precinct: {year: pop}}
Census key is read from env CENSUS_API_KEY (free; do not commit).
"""
import json, urllib.request, urllib.parse, io, zipfile, os, sys

KEY = os.environ.get("CENSUS_API_KEY", "")
if not KEY:
    _kf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".census_key")
    if os.path.exists(_kf):
        KEY = open(_kf).read().strip()
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
COUNTIES = {"005": "Bronx", "047": "Brooklyn", "061": "Manhattan", "081": "Queens", "085": "Staten Island"}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "precinct-pop/1.0"})
    return urllib.request.urlopen(req, timeout=180).read()


def fetch_tract_pop(year):
    """Return {tract_geoid(11): pop} for all NYC counties."""
    out = {}
    for cty in COUNTIES:
        if year == 2020:
            base = "https://api.census.gov/data/2020/dec/pl"
            var = "P1_001N"
        else:
            base = "https://api.census.gov/data/2010/dec/sf1"
            var = "P001001"
        url = f"{base}?get={var}&for=tract:*&in=state:36+county:{cty}&key={KEY}"
        rows = json.loads(get(url))
        hdr = rows[0]
        for r in rows[1:]:
            rec = dict(zip(hdr, r))
            geoid = "36" + cty + rec["tract"]
            out[geoid] = int(rec[var])
    return out


def fetch_centroids(year):
    """Return {tract_geoid(11): (lat, lon)} from the Census gazetteer."""
    out = {}
    if year == 2020:
        txt = get("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2020_Gazetteer/2020_gaz_tracts_36.txt").decode("latin-1")
        lines = txt.splitlines()
        hdr = [h.strip() for h in lines[0].split("\t")]
        gi, la, lo = hdr.index("GEOID"), hdr.index("INTPTLAT"), hdr.index("INTPTLONG")
        for ln in lines[1:]:
            p = ln.split("\t")
            if len(p) <= lo:
                continue
            out[p[gi].strip()] = (float(p[la]), float(p[lo]))
    else:
        # 2010 gazetteer is national zip
        raw = get("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/Gaz_tracts_national.zip")
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = [n for n in zf.namelist() if n.lower().endswith(".txt")][0]
        txt = zf.read(name).decode("latin-1")
        lines = txt.splitlines()
        hdr = [h.strip() for h in lines[0].split("\t")]
        gi = hdr.index("GEOID"); la = hdr.index("INTPTLAT"); lo = hdr.index("INTPTLONG")
        for ln in lines[1:]:
            p = ln.split("\t")
            if len(p) <= lo:
                continue
            g = p[gi].strip()
            if g.startswith("36005") or g.startswith("36047") or g.startswith("36061") or g.startswith("36081") or g.startswith("36085"):
                out[g] = (float(p[la]), float(p[lo]))
    return out


def point_in_ring(lat, lon, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]   # lon, lat
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def build_precinct_polys(gj):
    polys = []
    for f in gj["features"]:
        try:
            p = int(f["properties"]["precinct"])
        except (TypeError, ValueError):
            continue
        geom = f["geometry"]
        rings = []
        parts = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in parts:
            rings.append(poly[0])   # outer ring
        # bbox for speed
        allpts = [pt for r in rings for pt in r]
        lons = [pt[0] for pt in allpts]; lats = [pt[1] for pt in allpts]
        polys.append((p, rings, (min(lons), min(lats), max(lons), max(lats))))
    return polys


def assign(lat, lon, polys):
    for p, rings, (mnx, mny, mxx, mxy) in polys:
        if lon < mnx or lon > mxx or lat < mny or lat > mxy:
            continue
        if any(point_in_ring(lat, lon, r) for r in rings):
            return p
    return None


def precinct_pop_for(year, polys):
    pop = fetch_tract_pop(year)
    cents = fetch_centroids(year)
    out = {}
    missing = 0
    for geoid, n in pop.items():
        c = cents.get(geoid)
        if not c:
            missing += 1
            continue
        pr = assign(c[0], c[1], polys)
        if pr is not None:
            out[pr] = out.get(pr, 0) + n
    print(f"  {year}: tracts={len(pop)} assigned, centroids missing={missing}, total pop={sum(out.values()):,}", file=sys.stderr)
    return out


def main():
    if not KEY:
        print("Set CENSUS_API_KEY", file=sys.stderr); sys.exit(1)
    gj = json.load(open(os.path.join(DATA, "precincts.geojson")))
    polys = build_precinct_polys(gj)
    print("Fetching 2020...", file=sys.stderr)
    p2020 = precinct_pop_for(2020, polys)
    print("Fetching 2010...", file=sys.stderr)
    p2010 = precinct_pop_for(2010, polys)

    precincts = sorted(set(p2020) | set(p2010))
    out = {}
    for p in precincts:
        a, b = p2010.get(p), p2020.get(p)
        if not a or not b:
            continue
        series = {}
        for y in range(2006, 2027):
            if y <= 2010:
                v = a + (b - a) * (y - 2010) / 10.0      # extrapolate back along 2010->2020 slope
            elif y >= 2020:
                v = b + (b - a) * (y - 2020) / 10.0      # extrapolate forward
            else:
                v = a + (b - a) * (y - 2010) / 10.0      # interpolate
            series[str(y)] = max(0, round(v))
        out[str(p)] = {"pop2010": a, "pop2020": b, "years": series}
    with open(os.path.join(DATA, "precinct_pop_years.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote precinct_pop_years.json for {len(out)} precincts", file=sys.stderr)
    # spot check: biggest growth
    growth = sorted(out.items(), key=lambda kv: (kv[1]["pop2020"] - kv[1]["pop2010"]) / kv[1]["pop2010"], reverse=True)
    print("Fastest-growing precincts 2010->2020:", file=sys.stderr)
    for p, d in growth[:5]:
        print(f"  pct {p}: {d['pop2010']:,} -> {d['pop2020']:,} (+{100*(d['pop2020']-d['pop2010'])/d['pop2010']:.0f}%)", file=sys.stderr)


if __name__ == "__main__":
    main()
