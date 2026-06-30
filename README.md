# Two decades of gun violence in New York City

An interactive map and chart set tracing twenty years of shootings in New York City — the long decline of the 2010s, the pandemic surge, and the recent fall — by precinct, time and the people involved.

## What it shows

- The long arc: shooting incidents per year, 2006–2026, with fatal/nonfatal and y-axis-baseline toggles.
- An interactive map with a year slider and play control, switching between police-precinct totals (choropleth) and individual shooting locations (24,126 mapped points), with a fatal-only filter.
- Borough trend lines, a day-and-hour heatmap, seasonality and day-of-week charts.
- Victim demographics (age, sex, race), the fatality rate over time, and offender demographics (with caveats).

## Data sources

| Dataset | NYC Open Data ID | Rows | Used for |
|---|---|---|---|
| Shootings (2006–Present) | `5ucz-vwe8` | 24,127 | Incident date, time, borough, precinct, location |
| Shooting Victims | `pztn-9bne` | 28,912 | Victim age, sex, race, fatal flag |
| Shooting Offenders | `gdk4-mbsv` | 18,901 | Offender age, sex, race |
| Police Precincts (shoreline-clipped) | `y76i-bdw7` | 78 | Choropleth boundaries |

Published as a collection in February 2026. Records run 2006-01-01 → 2026-03-31 (2026 is partial).

## Method and confidence

- Victims and offenders are joined to incidents on `incident_key`. An incident is **fatal** if at least one of its victims carries the statistical-murder flag. Charts of "shootings" count incidents; demographic charts count victim/offender records.
- **Coordinate fix:** in the source Shootings table the `latitude`/`longitude` columns are swapped; the pipeline corrects the orientation before mapping. One record lacks usable coordinates (mapped nowhere, still counted in charts).
- Junk age values (e.g. `1022`, `1822`) and inconsistent sex labels are normalized to clean categories; invalid ages fall into "Unknown."
- Precinct counts are **raw, not population-adjusted** — read the map as "where shootings occur," not personal risk.
- **Offender caveat:** offender records exist only when a suspect was identified (a minority of shootings, unevenly), so offender demographics describe *identified* shooters, an arrest-skewed subset. Confidence: **high** for incident counts, geography and victim demographics; **medium** for offender demographics.
- Nothing is modeled or projected — every figure is a direct count of published records.

## Build

```bash
python3 build_data.py     # pulls 4 datasets, writes data/shootings_agg.json, shootings_points.json, precincts.geojson
python3 -m http.server 8732   # then open http://localhost:8732
```

Python 3 standard library only. Fully static page (Leaflet + CARTO basemap from CDN); deploys as-is to any static host.

Built with AI assistance (Claude); all figures computed by `build_data.py` directly from the sources above. Independent civic-data project — not affiliated with the NYPD or the City of New York.
