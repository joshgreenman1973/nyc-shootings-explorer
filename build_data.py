#!/usr/bin/env python3
"""
NYC shootings data pipeline (2006-present).

Sources (NYC Open Data / Socrata), the new three-table collection published
2026-02-10:
  - Shootings:          5ucz-vwe8  (incident-level: date, time, boro, precinct, lat/lon)
  - Shooting Victims:   pztn-9bne  (one row per victim, joined by incident_key)
  - Shooting Offenders: gdk4-mbsv  (one row per offender, joined by incident_key)

Also fetches NYPD precinct boundaries (shoreline-clipped, City Planning) for the
choropleth: 78dh-3ptz.

Outputs (all in data/):
  shootings_agg.json    aggregates for all charts
  shootings_points.json compact incident points for the map [lat,lon,year,boroIdx,fatal]
  precincts.geojson     precinct polygons

Confidence: HIGH for counts by year/boro/precinct and victim demographics
(directly from published records). Incident "fatal" = at least one victim with
the statistical-murder flag set. ~1.6% of incidents lack lat/lon and are
excluded from the map only (still counted in all charts).
"""
import json, urllib.request, urllib.parse, datetime, collections, os, sys

DOMAIN = "https://data.cityofnewyork.us/resource"
GEO = "https://data.cityofnewyork.us/api/geospatial"
INCIDENTS = "5ucz-vwe8"
VICTIMS = "pztn-9bne"
OFFENDERS = "gdk4-mbsv"
PRECINCTS = "y76i-bdw7"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def fetch_all(dataset, select=None, order=None):
    rows, offset, page = [], 0, 50000
    while True:
        params = {"$limit": page, "$offset": offset}
        if select:
            params["$select"] = select
        if order:
            params["$order"] = order
        url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "shootings-build/1.0"})
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

print("Fetching incidents...", file=sys.stderr)
incidents = fetch_all(INCIDENTS, order="occur_date")
print(f"  {len(incidents)} incidents", file=sys.stderr)
print("Fetching victims...", file=sys.stderr)
victims = fetch_all(VICTIMS)
print(f"  {len(victims)} victims", file=sys.stderr)
print("Fetching offenders...", file=sys.stderr)
offenders = fetch_all(OFFENDERS)
print(f"  {len(offenders)} offenders", file=sys.stderr)

# --- incident -> fatal? (any victim flagged statistical murder) ---
fatal_incident = set()
victims_by_incident = collections.Counter()
for v in victims:
    k = v.get("incident_key")
    victims_by_incident[k] += 1
    if (v.get("stat_murder_flg") or "").strip().upper() == "Y":
        fatal_incident.add(k)

# --- aggregators ---
by_year = collections.Counter()
by_year_fatal = collections.Counter()
victims_by_year = collections.Counter()
victims_by_year_fatal = collections.Counter()
by_yearmonth = collections.Counter()
by_month = collections.Counter()            # seasonality (calendar month)
by_dow = collections.Counter()              # 0=Mon
by_hour = collections.Counter()
hour_dow = collections.Counter()            # (dow, hour) heatmap
boro_year = collections.defaultdict(collections.Counter)   # boro -> year -> count
precinct_total = collections.Counter()
precinct_fatal = collections.Counter()
precinct_recent = collections.Counter()     # last 3 full years (2023-2025)
precinct_year = collections.defaultdict(collections.Counter)  # precinct -> year -> count
loc_class = collections.Counter()
points = []
n_no_geo = 0

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

for r in incidents:
    d = r.get("occur_date", "")[:10]
    if not d:
        continue
    try:
        dt = datetime.date.fromisoformat(d)
    except ValueError:
        continue
    key = r.get("incident_key")
    fatal = key in fatal_incident
    yr = dt.year
    boro = (r.get("boro") or "").strip().upper()
    prec = r.get("precinct")
    nvic = max(1, victims_by_incident.get(key, 1))

    by_year[yr] += 1
    if fatal:
        by_year_fatal[yr] += 1
    victims_by_year[yr] += nvic
    by_yearmonth[f"{yr:04d}-{dt.month:02d}"] += 1
    by_month[dt.month] += 1
    dow = dt.weekday()
    by_dow[dow] += 1
    boro_year[boro][yr] += 1
    if prec:
        precinct_total[prec] += 1
        if fatal:
            precinct_fatal[prec] += 1
        if yr >= 2023 and yr <= 2025:
            precinct_recent[prec] += 1
        precinct_year[prec][yr] += 1
    lc = (r.get("loc_classfctn_desc") or r.get("location_desc") or "").strip()
    if lc:
        loc_class[lc] += 1

    # time of day
    t = (r.get("occur_time") or "")
    hr = None
    if t and ":" in t:
        try:
            hr = int(t.split(":")[0])
        except ValueError:
            hr = None
    if hr is not None and 0 <= hr <= 23:
        by_hour[hr] += 1
        hour_dow[(dow, hr)] += 1

    # NOTE: this dataset has latitude/longitude column labels SWAPPED at the
    # source (the "latitude" field holds ~-73.9, "longitude" holds ~40.6).
    # We read them in the corrected orientation.
    lat, lon = num(r.get("longitude")), num(r.get("latitude"))
    if lat is None or lon is None or not (40.4 < lat < 41.0) or not (-74.3 < lon < -73.6):
        n_no_geo += 1
    else:
        points.append([round(lat, 5), round(lon, 5), yr,
                       BORO_IDX.get(boro, -1), 1 if fatal else 0])

# victim-level fatal counts by year
for v in victims:
    k = v.get("incident_key")
    # find incident year via a lookup map
# Build incident-year map once
inc_year = {}
for r in incidents:
    d = r.get("occur_date", "")[:10]
    if d:
        try:
            inc_year[r.get("incident_key")] = datetime.date.fromisoformat(d).year
        except ValueError:
            pass
for v in victims:
    k = v.get("incident_key")
    y = inc_year.get(k)
    if y is None:
        continue
    if (v.get("stat_murder_flg") or "").strip().upper() == "Y":
        victims_by_year_fatal[y] += 1


VALID_AGE = {"<18", "18-24", "25-44", "45-64", "65+"}
SEX_MAP = {"MALE": "Male", "M": "Male", "FEMALE": "Female", "F": "Female"}


def demo(rows, age_f, sex_f, race_f):
    age = collections.Counter()
    sex = collections.Counter()
    race = collections.Counter()
    for r in rows:
        a = (r.get(age_f) or "").strip().upper()
        a = a if a in VALID_AGE else "Unknown"   # drop junk ages (1022, 1822, blanks)
        s = SEX_MAP.get((r.get(sex_f) or "").strip().upper(), "Unknown")
        rc = (r.get(race_f) or "").strip() or "UNKNOWN"
        age[a] += 1
        sex[s] += 1
        race[rc] += 1
    return age, sex, race


v_age, v_sex, v_race = demo(victims, "victim_age_group", "victim_sex", "victim_race")
o_age, o_sex, o_race = demo(offenders, "perp_age_group", "perp_sex", "perp_race")

AGE_ORDER = ["<18", "18-24", "25-44", "45-64", "65+", "Unknown"]


def order_counter(c, order):
    keys = [k for k in order if k in c] + [k for k in c if k not in order]
    return [{"label": k, "n": c[k]} for k in keys]


def top_counter(c, n=12):
    return [{"label": k, "n": v} for k, v in c.most_common(n)]


years = sorted(by_year)
agg = {
    "meta": {
        "generated": "2026-06-30",
        "n_incidents": len(incidents),
        "n_victims": len(victims),
        "n_offenders": len(offenders),
        "n_mapped": len(points),
        "n_no_geo": n_no_geo,
        "date_min": min((r.get("occur_date", "") for r in incidents), default="")[:10],
        "date_max": max((r.get("occur_date", "") for r in incidents), default="")[:10],
        "boros": BOROS,
        "sources": {
            "incidents": f"{DOMAIN}/{INCIDENTS}",
            "victims": f"{DOMAIN}/{VICTIMS}",
            "offenders": f"{DOMAIN}/{OFFENDERS}",
            "precincts": f"{DOMAIN}/{PRECINCTS}",
        },
    },
    "by_year": [{"year": y, "incidents": by_year[y], "fatal": by_year_fatal.get(y, 0),
                 "victims": victims_by_year.get(y, 0),
                 "victims_fatal": victims_by_year_fatal.get(y, 0)} for y in years],
    "by_yearmonth": [{"ym": k, "n": by_yearmonth[k]} for k in sorted(by_yearmonth)],
    "by_month": [{"month": MONTH_NAMES[m - 1], "n": by_month.get(m, 0)} for m in range(1, 13)],
    "by_dow": [{"dow": DOW_NAMES[i], "n": by_dow.get(i, 0)} for i in range(7)],
    "by_hour": [{"hour": h, "n": by_hour.get(h, 0)} for h in range(24)],
    "hour_dow": [{"dow": i, "hour": h, "n": hour_dow.get((i, h), 0)}
                 for i in range(7) for h in range(24)],
    "boro_year": [{"boro": b, "series": [{"year": y, "n": boro_year[b].get(y, 0)} for y in years]}
                  for b in BOROS],
    "precinct": [{"precinct": int(p) if str(p).isdigit() else p,
                  "total": precinct_total[p], "fatal": precinct_fatal[p],
                  "recent": precinct_recent.get(p, 0),
                  "yr": {str(y): precinct_year[p][y] for y in precinct_year[p]}}
                 for p in precinct_total],
    "years": years,
    "loc_class": top_counter(loc_class, 10),
    "victim_age": order_counter(v_age, AGE_ORDER),
    "victim_sex": order_counter(v_sex, ["Male", "Female", "Unknown"]),
    "victim_race": top_counter(v_race, 12),
    "offender_age": order_counter(o_age, AGE_ORDER),
    "offender_sex": order_counter(o_sex, ["Male", "Female", "Unknown"]),
    "offender_race": top_counter(o_race, 12),
}

os.makedirs(DATA, exist_ok=True)
with open(os.path.join(DATA, "shootings_agg.json"), "w") as f:
    json.dump(agg, f, separators=(",", ":"))
with open(os.path.join(DATA, "shootings_points.json"), "w") as f:
    json.dump({"boros": BOROS, "points": points}, f, separators=(",", ":"))

# --- precinct geometry ---
print("Fetching precinct geometry...", file=sys.stderr)
geo_url = f"{GEO}/{PRECINCTS}?method=export&format=GeoJSON"
try:
    req = urllib.request.Request(geo_url, headers={"User-Agent": "shootings-build/1.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        gj = json.load(r)
    with open(os.path.join(DATA, "precincts.geojson"), "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    print(f"  precincts.geojson: {len(gj.get('features', []))} features", file=sys.stderr)
except Exception as e:
    print(f"  WARN: precinct geometry fetch failed: {e}", file=sys.stderr)

for fn in ("shootings_agg.json", "shootings_points.json"):
    p = os.path.join(DATA, fn)
    print(f"Wrote {fn} ({os.path.getsize(p)//1024} KB)", file=sys.stderr)
print(f"Mapped points: {len(points)}  | no-geo: {n_no_geo}", file=sys.stderr)
