# ⚡ EV Charger SA

A Streamlit app for finding EV charging stations across **South Africa**.
Find chargers near you with availability and speed tiers, filter by connector
type (CCS, CHAdeMO, Type 2, Tesla), or plan a road trip with fast-charging
stops placed along the route based on your EV's range — with live station
status (where available) and modelled wait-time estimates.

Data sourced from [Open Charge Map](https://openchargemap.org), which includes
stations from GridCars, Charge, and other SA networks.

## What it does

- **Find a charger** — search stations around any SA address or city, filter by
  speed tier, connector type, and likely availability, and get per-station speed labels
  (Hyper-Fast / Ultra-Fast / Fast / AC / Level 2), bays-likely-open counts,
  wait estimates and one-tap Google Maps directions.
- **Route** — geocodes your start and destination within South Africa
  (OpenStreetMap), lets you confirm which match you meant, and draws the
  actual driving route (OSRM).
- **Charging stops** — pulls chargers from Open Charge Map, filters to those
  within a corridor of your route, and greedily places fast-charge stops so you
  never run below a 15% reserve. Charging time per stop is estimated from the
  energy actually needed (battery size, arrival charge, charger power), and
  each stop shows how far off the route it sits.
- **Wait-time estimates** — uses Open Charge Map's live operational status where
  it's available (e.g. flagging out-of-service sites) and models expected wait
  from charger power, number of bays, and time-of-day demand. Wait *numbers* are
  estimates, clearly labelled; operational status is live where OCM provides it.
- **Trip summary** — total distance, drive time, number of stops, and combined
  charging + waiting time.

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

It opens at http://localhost:8501.

### Open Charge Map API key (required)

Open Charge Map requires an API key for all requests. Get a free key at
<https://openchargemap.org/site/develop/> and paste it into the sidebar, or add
it to `.streamlit/secrets.toml`:

```toml
ocm_api_key = "your-key-here"
```

## Project structure

```
ev_locator/
├── app.py             # Streamlit UI (inputs, map, summary, tables)
├── ev_core.py         # data + logic: geocoding, routing, OCM, planning, waits
├── requirements.txt
├── tests/
│   └── test_ev_core.py  # unit tests (pure logic + mocked API failures)
├── .streamlit/
│   └── secrets.toml   # ocm_api_key (git-ignored — never commit)
└── README.md
```

## Tests

```bash
pip install pytest
pytest
```

No network access needed — external APIs are mocked.

## How wait times are estimated

For each charger the model computes a per-bay utilisation from a time-of-day
demand curve (twin commute peaks), damped by the number of bays, then converts
that to an expected wait using a simple queueing approximation. The same
utilisation drives a **bays open** estimate (bays minus expected bays in use);
out-of-service sites show 0 open from live status. Faster chargers
imply shorter sessions and quicker turnover. Sites that Open Charge Map reports
as not operational are shown as **Out of service** from live data.

## Limits (honest prototype notes)

- Routing and geocoding use free public demo servers (OSRM, Nominatim) which are
  rate-limited and occasionally slow.
- Open Charge Map's free API does not expose real-time bay occupancy, so
  expected wait is modelled, not measured.
- Charge-stop planning uses a range-based heuristic (rated range × reserve).
  Charge times are energy-based (battery size × state of charge, damped by an
  average taper factor) but not a full per-model charge-curve simulation.
- The corridor charger filter uses a vertex-based distance approximation for
  speed.
