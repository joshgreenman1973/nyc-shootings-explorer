#!/usr/bin/env python3
"""
NYC shootings data pipeline v2 (2006-present) — trends-over-time edition.

Sources (NYC Open Data / Socrata), the three-table collection published 2026-02-10:
  - Shootings:          5ucz-vwe8  (incident-level)
  - Shooting Victims:   pztn-9bne  (one row per victim)
  - Shooting Offenders: gdk4-mbsv  (one row per offender)
  - Police Precincts:   y76i-bdw7  (shoreline-clipped boundaries)

This version adds everything needed to show how patterns CHANGED over the years
rather than only the all-time totals:
  * Demographics BY YEAR (victim + offender age/sex/race shares) so shifts show.
  * Precinct ("neighborhood") change analysis: share of citywide shootings in a
    pre-pandemic window vs a recent window, the biggest pandemic spikes, and the
    biggest declines.
  * Simplified, projected SVG outlines per precinct so the front end can draw a
    small-multiples grid (one mini-map per year) with no map tiles at all.

Outputs (data/):
  shootings_agg.json    everything for charts + change tables + precinct shapes
  shootings_points.json compact incident points for the optional dot map
  precincts.geojson     full-resolution polygons for the interactive Leaflet map

Confidence: HIGH for counts/geography/victim demographics; MEDIUM for offender
demographics (identified-suspect subset). Neighborhood labels are approximate
precinct nicknames for readability, not official boundaries.
"""
import json, urllib.request, urllib.parse, datetime, collections, os, sys, math, csv

DOMAIN = "https://data.cityofnewyork.us/resource"
GEO = "https://data.cityofnewyork.us/api/geospatial"
INCIDENTS, VICTIMS, OFFENDERS, PRECINCTS = "5ucz-vwe8", "pztn-9bne", "gdk4-mbsv", "y76i-bdw7"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# Approximate precinct -> neighborhood labels (for readability).
PRECINCT_NAME = {
    1: "Financial District", 5: "Chinatown", 6: "West Village", 7: "Lower East Side",
    9: "East Village", 10: "Chelsea", 13: "Gramercy", 14: "Midtown South",
    17: "Murray Hill", 18: "Midtown North", 19: "Upper East Side", 20: "Upper West Side",
    22: "Central Park", 23: "East Harlem", 24: "UWS North", 25: "East Harlem North",
    26: "Morningside Hts", 28: "Central Harlem S", 30: "Hamilton Heights",
    32: "Central Harlem N", 33: "Washington Hts", 34: "Wash. Hts / Inwood",
    40: "Mott Haven", 41: "Hunts Point", 42: "Morrisania", 43: "Soundview",
    44: "Highbridge", 45: "Throgs Neck", 46: "Fordham", 47: "Wakefield",
    48: "East Tremont", 49: "Pelham Parkway", 50: "Kingsbridge", 52: "Bedford Park",
    60: "Coney Island", 61: "Sheepshead Bay", 62: "Bensonhurst", 63: "Marine Park",
    66: "Borough Park", 67: "East Flatbush", 68: "Bay Ridge", 69: "Canarsie",
    70: "Flatbush", 71: "Crown Heights S", 72: "Sunset Park", 73: "Brownsville",
    75: "East New York", 76: "Red Hook", 77: "Crown Heights", 78: "Park Slope",
    79: "Bed-Stuy South", 81: "Bed-Stuy North", 83: "Bushwick", 84: "Downtown Bklyn",
    88: "Fort Greene", 90: "Williamsburg S", 94: "Greenpoint", 100: "Rockaways",
    101: "Far Rockaway", 102: "Richmond Hill", 103: "Jamaica", 104: "Ridgewood",
    105: "Queens Village", 106: "Ozone Park", 107: "Fresh Meadows",
    108: "Long Island City", 109: "Flushing", 110: "Elmhurst", 111: "Bayside",
    112: "Forest Hills", 113: "St. Albans", 114: "Astoria", 115: "Jackson Heights",
    116: "Rosedale", 120: "St. George", 121: "Mariners Harbor", 122: "New Dorp",
    123: "Tottenville",
}


def fetch_all(dataset, order=None):
    rows, offset, page = [], 0, 50000
    while True:
        params = {"$limit": page, "$offset": offset}
        if order:
            params["$order"] = order
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "shootings-build/2.0"})
        with urllib.request.urlopen(req, timeout=180) as r:
            batch = json.load(r)
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


BOROS = ["BRONX", "BROOKLYN", "MANHATTAN", "QUEENS", "STATEN ISLAND"]
BORO_IDX = {b: i for i, b in enumerate(BOROS)}
VALID_AGE = ["<18", "18-24", "25-44", "45-64", "65+"]
SEX_MAP = {"MALE": "Male", "M": "Male", "FEMALE": "Female", "F": "Female"}


def clean_age(v):
    v = (v or "").strip().upper()
    return v if v in VALID_AGE else "Unknown"


def clean_sex(v):
    return SEX_MAP.get((v or "").strip().upper(), "Unknown")


def clean_race(v):
    return (v or "").strip().upper() or "UNKNOWN"


# Precinct resident population, 2020 Census (P1_001N), via John Keefe's
# census-by-precincts project. Used for per-100k-resident rates.
# https://github.com/jkeefe/census-by-precincts
PRECINCT_POP = {}
pop_path = os.path.join(DATA, "precinct_pop_2020.csv")
if os.path.exists(pop_path):
    for row in csv.DictReader(open(pop_path)):
        try:
            PRECINCT_POP[int(row["precinct"])] = int(row["P1_001N"])
        except (TypeError, ValueError):
            pass
# Precincts with too few residents for a meaningful rate (parks, business cores)
LOW_POP = {p for p, n in PRECINCT_POP.items() if n < 5000}

print("Fetching incidents / victims / offenders...", file=sys.stderr)
incidents = fetch_all(INCIDENTS, order="occur_date")
victims = fetch_all(VICTIMS)
offenders = fetch_all(OFFENDERS)
print(f"  {len(incidents)} / {len(victims)} / {len(offenders)}", file=sys.stderr)
print(f"  precinct pop loaded: {len(PRECINCT_POP)} (low-pop excluded from rates: {sorted(LOW_POP)})", file=sys.stderr)

# incident -> year, boro, precinct, fatal
inc_year, inc_meta = {}, {}
fatal_incident = set()
victims_by_incident = collections.Counter()
for v in victims:
    k = v.get("incident_key")
    victims_by_incident[k] += 1
    if (v.get("stat_murder_flg") or "").strip().upper() == "Y":
        fatal_incident.add(k)

# aggregators
by_year = collections.Counter()
by_year_fatal = collections.Counter()
victims_by_year = collections.Counter()
victims_by_year_fatal = collections.Counter()
by_yearmonth = collections.Counter()
by_month = collections.Counter()
by_dow = collections.Counter()
by_hour = collections.Counter()
hour_dow = collections.Counter()
# night share over time: share of shootings 8pm-4am
night_by_year = collections.Counter()
boro_year = collections.defaultdict(collections.Counter)
precinct_year = collections.defaultdict(collections.Counter)
precinct_total = collections.Counter()
precinct_fatal = collections.Counter()
# casualties (people shot per incident) — the only available proxy for severity,
# since the dataset has no shots-fired field
vpi_dist = collections.Counter()                 # victims-per-incident -> n incidents
multi_by_year = collections.Counter()            # incidents with 2+ victims, by year
victims_total_by_year = collections.Counter()    # already have victims_by_year; keep
points = []
n_no_geo = 0

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

for r in incidents:
    d = (r.get("occur_date") or "")[:10]
    if not d:
        continue
    try:
        dt = datetime.date.fromisoformat(d)
    except ValueError:
        continue
    key = r.get("incident_key")
    fatal = key in fatal_incident
    yr, mo, dow = dt.year, dt.month, dt.weekday()
    boro = (r.get("boro") or "").strip().upper()
    prec = r.get("precinct")
    try:
        prec = int(prec)
    except (TypeError, ValueError):
        prec = None
    inc_year[key] = yr

    by_year[yr] += 1
    if fatal:
        by_year_fatal[yr] += 1
    nv = max(1, victims_by_incident.get(key, 1))
    victims_by_year[yr] += nv
    vpi_dist[min(nv, 5)] += 1               # cap bucket at "5+"
    if nv >= 2:
        multi_by_year[yr] += 1
    by_yearmonth[f"{yr:04d}-{mo:02d}"] += 1
    by_month[mo] += 1
    by_dow[dow] += 1
    boro_year[boro][yr] += 1
    if prec is not None:
        precinct_year[prec][yr] += 1
        precinct_total[prec] += 1
        if fatal:
            precinct_fatal[prec] += 1

    t = (r.get("occur_time") or "")
    hr = None
    if ":" in t:
        try:
            hr = int(t.split(":")[0])
        except ValueError:
            hr = None
    if hr is not None and 0 <= hr <= 23:
        by_hour[hr] += 1
        hour_dow[(dow, hr)] += 1
        if hr >= 20 or hr < 4:
            night_by_year[yr] += 1

    lat, lon = num(r.get("longitude")), num(r.get("latitude"))   # NOTE: source columns swapped
    if lat is None or lon is None or not (40.4 < lat < 41.0) or not (-74.3 < lon < -73.6):
        n_no_geo += 1
    else:
        points.append([round(lat, 5), round(lon, 5), yr, BORO_IDX.get(boro, -1), 1 if fatal else 0])

# victim fatal counts by year
for v in victims:
    y = inc_year.get(v.get("incident_key"))
    if y is None:
        continue
    if (v.get("stat_murder_flg") or "").strip().upper() == "Y":
        victims_by_year_fatal[y] += 1

years = sorted(by_year)
RECENT = [y for y in years if y <= 2025]  # exclude partial 2026 from "recent" windows

# ---------- demographics by year (shares) ----------
def demo_by_year(rows, age_f, sex_f, race_f):
    age = collections.defaultdict(collections.Counter)   # year -> agebucket -> n
    race = collections.defaultdict(collections.Counter)
    tot = collections.Counter()
    for r in rows:
        y = inc_year.get(r.get("incident_key"))
        if y is None:
            continue
        age[y][clean_age(r.get(age_f))] += 1
        race[y][clean_race(r.get(race_f))] += 1
        tot[y] += 1
    return age, race, tot

v_age_y, v_race_y, v_tot_y = demo_by_year(victims, "victim_age_group", "victim_sex", "victim_race")
o_age_y, o_race_y, o_tot_y = demo_by_year(offenders, "perp_age_group", "perp_sex", "perp_race")

def share_series(age_y, tot_y, buckets):
    # returns {bucket: [{year, share, n}]}
    out = {}
    for b in buckets:
        out[b] = [{"year": y, "n": age_y[y].get(b, 0),
                   "share": round(100 * age_y[y].get(b, 0) / tot_y[y], 1) if tot_y[y] else None}
                  for y in years]
    return out

TOP_RACES = ["BLACK", "WHITE HISPANIC", "BLACK HISPANIC", "WHITE", "ASIAN / PACIFIC ISLANDER"]

# all-time demographic totals (cleaned)
def totals(rows, age_f, sex_f, race_f):
    age, sex, race = collections.Counter(), collections.Counter(), collections.Counter()
    for r in rows:
        age[clean_age(r.get(age_f))] += 1
        sex[clean_sex(r.get(sex_f))] += 1
        race[clean_race(r.get(race_f))] += 1
    return age, sex, race

va, vs, vr = totals(victims, "victim_age_group", "victim_sex", "victim_race")
oa, os_, orc = totals(offenders, "perp_age_group", "perp_sex", "perp_race")

def order_c(c, order):
    keys = [k for k in order if k in c] + [k for k in c if k not in order]
    return [{"label": k, "n": c[k]} for k in keys]

def top_c(c, n=8):
    return [{"label": k, "n": v} for k, v in c.most_common(n)]

# ---------- precinct change analysis ----------
def window_avg(prec, yrs):
    return sum(precinct_year[prec].get(y, 0) for y in yrs) / len(yrs)

city_by_year = {y: by_year[y] for y in years}
PRE = [2015, 2016, 2017, 2018, 2019]      # pre-pandemic baseline
POST = [2021, 2022, 2023, 2024, 2025]     # recent
PAN_BASE = [2018, 2019]
PAN_PEAK = [2020, 2021]
DECLINE_PEAK = [2020, 2021]
DECLINE_RECENT = [2023, 2024, 2025]

city_pre = sum(city_by_year.get(y, 0) for y in PRE) / len(PRE)
city_post = sum(city_by_year.get(y, 0) for y in POST) / len(POST)

prec_changes = []
for p in precinct_total:
    if precinct_total[p] < 60:   # skip very low-volume precincts (Midtown, etc.) for stability
        continue
    pre = window_avg(p, PRE)
    post = window_avg(p, POST)
    share_pre = 100 * pre / city_pre if city_pre else 0
    share_post = 100 * post / city_post if city_post else 0
    pan_base = window_avg(p, PAN_BASE)
    pan_peak = window_avg(p, PAN_PEAK)
    dec_peak = window_avg(p, DECLINE_PEAK)
    dec_recent = window_avg(p, DECLINE_RECENT)
    pop = PRECINCT_POP.get(p)
    has_rate = pop and p not in LOW_POP
    prec_changes.append({
        "precinct": p, "name": PRECINCT_NAME.get(p, f"Precinct {p}"),
        "total": precinct_total[p], "pop": pop,
        "share_pre": round(share_pre, 2), "share_post": round(share_post, 2),
        "share_change": round(share_post - share_pre, 2),
        "pandemic_pct": round(100 * (pan_peak - pan_base) / pan_base, 0) if pan_base >= 3 else None,
        "pandemic_abs": round(pan_peak - pan_base, 1),
        "decline_pct": round(100 * (dec_recent - dec_peak) / dec_peak, 0) if dec_peak >= 3 else None,
        "pre_avg": round(pre, 1), "post_avg": round(post, 1),
        "peak_avg": round(dec_peak, 1), "recent_avg": round(dec_recent, 1),
        # annual shootings per 100k residents, recent window (2021-25)
        "rate_post": round(post / pop * 100000, 1) if has_rate else None,
        "rate_pre": round(pre / pop * 100000, 1) if has_rate else None,
    })

# ---------- simplified, projected SVG paths for small multiples ----------
print("Fetching precinct geometry...", file=sys.stderr)
geo_url = f"{GEO}/{PRECINCTS}?method=export&format=GeoJSON"
gj = None
try:
    req = urllib.request.Request(geo_url, headers={"User-Agent": "shootings-build/2.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        gj = json.load(r)
    with open(os.path.join(DATA, "precincts.geojson"), "w") as f:
        json.dump(gj, f, separators=(",", ":"))
except Exception as e:
    print(f"  WARN: geometry fetch failed: {e}", file=sys.stderr)

def rdp(points, eps):
    """Ramer-Douglas-Peucker polyline simplification."""
    if len(points) < 3:
        return points
    dmax, idx = 0.0, 0
    a, b = points[0], points[-1]
    for i in range(1, len(points) - 1):
        d = perp_dist(points[i], a, b)
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        left = rdp(points[:idx + 1], eps)
        right = rdp(points[idx:], eps)
        return left[:-1] + right
    return [a, b]

def perp_dist(p, a, b):
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))

precinct_paths = {}
VIEW_W = 1000.0
if gj:
    # bounds
    lons, lats = [], []
    for f in gj["features"]:
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            for ring in poly:
                for x, y in ring:
                    lons.append(x); lats.append(y)
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    latm = math.radians((lat0 + lat1) / 2)
    sx = VIEW_W / (lon1 - lon0)
    sy = sx * math.cos(latm)            # aspect correction
    VIEW_H = round((lat1 - lat0) * sy, 1)
    def project(x, y):
        return (round((x - lon0) * sx, 1), round((lat1 - y) * sy, 1))
    eps = 0.4   # simplification tolerance in projected px
    for f in gj["features"]:
        try:
            p = int(f["properties"]["precinct"])
        except (TypeError, ValueError):
            continue
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        d = ""
        for poly in polys:
            ring = poly[0]   # outer ring only (drop holes for small multiples)
            pts = [project(x, y) for x, y in ring]
            pts = rdp(pts, eps)
            if len(pts) < 3:
                continue
            d += "M" + " L".join(f"{x} {y}" for x, y in pts) + "Z"
        if d:
            precinct_paths[str(p)] = d
    print(f"  {len(precinct_paths)} precinct paths, viewbox {VIEW_W}x{VIEW_H}", file=sys.stderr)

# ---------- assemble ----------
agg = {
    "meta": {
        "generated": "2026-06-30",
        "n_incidents": len(incidents), "n_victims": len(victims), "n_offenders": len(offenders),
        "n_mapped": len(points), "n_no_geo": n_no_geo,
        "date_min": (min((r.get("occur_date", "") for r in incidents), default=""))[:10],
        "date_max": (max((r.get("occur_date", "") for r in incidents), default=""))[:10],
        "boros": BOROS, "years": years,
        "view_w": VIEW_W, "view_h": VIEW_H if gj else 0,
        "windows": {"pre": PRE, "post": POST, "pandemic_base": PAN_BASE, "pandemic_peak": PAN_PEAK,
                    "decline_peak": DECLINE_PEAK, "decline_recent": DECLINE_RECENT},
        "sources": {"incidents": f"{DOMAIN}/{INCIDENTS}", "victims": f"{DOMAIN}/{VICTIMS}",
                    "offenders": f"{DOMAIN}/{OFFENDERS}", "precincts": f"{DOMAIN}/{PRECINCTS}"},
    },
    "by_year": [{"year": y, "incidents": by_year[y], "fatal": by_year_fatal.get(y, 0),
                 "victims": victims_by_year.get(y, 0), "victims_fatal": victims_by_year_fatal.get(y, 0),
                 "fatal_rate": round(100 * by_year_fatal.get(y, 0) / by_year[y], 1) if by_year[y] else None,
                 "night_share": round(100 * night_by_year.get(y, 0) / by_year[y], 1) if by_year[y] else None,
                 "multi_share": round(100 * multi_by_year.get(y, 0) / by_year[y], 1) if by_year[y] else None,
                 "victims_per_incident": round(victims_by_year.get(y, 0) / by_year[y], 3) if by_year[y] else None}
                for y in years],
    "casualties": {
        "no_shots_fired_field": True,   # dataset has no shots-fired count; victims is the proxy
        "vpi_dist": [{"victims": v if v < 5 else "5+", "incidents": vpi_dist.get(v, 0)} for v in range(1, 6)],
        "multi_total": sum(multi_by_year.values()),
        "city_pop_2020": sum(PRECINCT_POP.values()),
    },
    "by_month": [{"month": MON[m - 1], "n": by_month.get(m, 0)} for m in range(1, 13)],
    "by_dow": [{"dow": DOW[i], "n": by_dow.get(i, 0)} for i in range(7)],
    "by_hour": [{"hour": h, "n": by_hour.get(h, 0)} for h in range(24)],
    "hour_dow": [{"dow": i, "hour": h, "n": hour_dow.get((i, h), 0)} for i in range(7) for h in range(24)],
    "boro_year": [{"boro": b, "series": [{"year": y, "n": boro_year[b].get(y, 0)} for y in years]} for b in BOROS],
    # all-time demographics
    "victim_age": order_c(va, VALID_AGE + ["Unknown"]),
    "victim_sex": order_c(vs, ["Male", "Female", "Unknown"]),
    "victim_race": top_c(vr, 8),
    "offender_age": order_c(oa, VALID_AGE + ["Unknown"]),
    "offender_sex": order_c(os_, ["Male", "Female", "Unknown"]),
    "offender_race": top_c(orc, 8),
    # demographics over time (shares)
    "victim_age_trend": share_series(v_age_y, v_tot_y, VALID_AGE),
    "victim_race_trend": share_series(v_race_y, v_tot_y, TOP_RACES),
    "offender_age_trend": share_series(o_age_y, o_tot_y, VALID_AGE),
    # precinct change analysis + geometry
    "precinct": [{"precinct": p, "name": PRECINCT_NAME.get(p, f"Precinct {p}"),
                  "total": precinct_total[p], "fatal": precinct_fatal[p],
                  "pop": PRECINCT_POP.get(p), "low_pop": p in LOW_POP,
                  "yr": {str(y): precinct_year[p].get(y, 0) for y in precinct_year[p]}}
                 for p in precinct_total],
    "precinct_changes": prec_changes,
    "precinct_paths": precinct_paths,
}

os.makedirs(DATA, exist_ok=True)
with open(os.path.join(DATA, "shootings_agg.json"), "w") as f:
    json.dump(agg, f, separators=(",", ":"))
with open(os.path.join(DATA, "shootings_points.json"), "w") as f:
    json.dump({"boros": BOROS, "points": points}, f, separators=(",", ":"))

for fn in ("shootings_agg.json", "shootings_points.json"):
    print(f"Wrote {fn} ({os.path.getsize(os.path.join(DATA, fn))//1024} KB)", file=sys.stderr)
print(f"Mapped points: {len(points)} | no-geo: {n_no_geo}", file=sys.stderr)
# quick previews
top_share = sorted(prec_changes, key=lambda c: c["share_change"], reverse=True)[:3]
bot_share = sorted(prec_changes, key=lambda c: c["share_change"])[:3]
print("Biggest share gains:", [(c["name"], c["share_change"]) for c in top_share], file=sys.stderr)
print("Biggest share drops:", [(c["name"], c["share_change"]) for c in bot_share], file=sys.stderr)
