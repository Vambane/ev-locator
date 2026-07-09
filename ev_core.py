"""
ev_core.py — data + logic layer for the EV route planner.

Responsibilities:
  * Geocoding (OpenStreetMap / Nominatim)
  * Driving routes (OSRM public demo server)
  * Charger data along a route corridor (Open Charge Map)
  * Charge-stop planning along the route (range-based)
  * Wait-time estimation (uses OCM live status where present, else a model)

Everything talks to free/public endpoints, so treat results as best-effort.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
OCM_URL = "https://api.openchargemap.io/v3/poi/"

# Polite identification for the free OSM services.
USER_AGENT = "ev-route-planner/1.0 (personal project)"
REQUEST_TIMEOUT = 20


class ChargerAPIError(Exception):
    """Open Charge Map request failed; message is safe to show the user."""


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class Place:
    name: str
    lat: float
    lon: float


@dataclass
class Route:
    coords: list[tuple[float, float]]          # list of (lat, lon)
    distance_km: float
    duration_min: float


@dataclass
class Charger:
    id: int
    name: str
    lat: float
    lon: float
    address: str
    town: str
    max_power_kw: float
    num_points: int
    connectors: list[str]
    is_operational: Optional[bool]             # from OCM status; None = unknown
    status_title: str
    # Filled in by wait-time model:
    dist_along_km: float = 0.0
    busyness_pct: int = 0
    wait_band: str = ""
    wait_minutes_est: Optional[int] = None
    wait_source: str = ""                      # "live" | "modelled"
    charge_time_min: int = 0
    bays_open_est: Optional[int] = None        # modelled; 0 if out of service
    off_route_km: float = 0.0                  # one-way distance off the route


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, in km."""
    r = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _cumulative_distances(coords: list[tuple[float, float]]) -> list[float]:
    """Cumulative distance (km) at each vertex of the route polyline."""
    cum = [0.0]
    for i in range(1, len(coords)):
        cum.append(cum[-1] + haversine_km(coords[i - 1], coords[i]))
    return cum


def _min_dist_to_route_km(point: tuple[float, float],
                          coords: list[tuple[float, float]],
                          cum: list[float]) -> tuple[float, float]:
    """
    Return (min distance from point to the polyline, distance along route at
    the nearest vertex). Vertex-based approximation — good enough for a
    corridor filter without pulling in shapely.
    """
    best_d = float("inf")
    best_along = 0.0
    for i, v in enumerate(coords):
        d = haversine_km(point, v)
        if d < best_d:
            best_d = d
            best_along = cum[i]
    return best_d, best_along


# --------------------------------------------------------------------------- #
# External services
# --------------------------------------------------------------------------- #

def geocode_candidates(query: str, limit: int = 5) -> list[Place]:
    """
    Resolve a place name to a ranked list of candidate places via Nominatim.
    Multiple candidates let the UI confirm *which* "Springfield" was meant
    instead of silently taking the top hit.
    """
    if not query or not query.strip():
        return []
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": limit},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    return [Place(name=item.get("display_name", query),
                  lat=float(item["lat"]), lon=float(item["lon"]))
            for item in data]


def geocode(query: str) -> Optional[Place]:
    """Resolve a place name to the single best-match place (top candidate)."""
    candidates = geocode_candidates(query, limit=1)
    return candidates[0] if candidates else None


def get_route(start: Place, end: Place) -> Optional[Route]:
    """Driving route between two places via the OSRM public server."""
    url = f"{OSRM_URL}/{start.lon},{start.lat};{end.lon},{end.lat}"
    try:
        resp = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    if data.get("code") != "Ok" or not data.get("routes"):
        return None
    route = data["routes"][0]
    # GeoJSON is [lon, lat]; convert to (lat, lon).
    coords = [(c[1], c[0]) for c in route["geometry"]["coordinates"]]
    return Route(coords=coords,
                 distance_km=route["distance"] / 1000.0,
                 duration_min=route["duration"] / 60.0)


def _bbox(coords: list[tuple[float, float]], pad_deg: float = 0.15):
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return (min(lats) - pad_deg, min(lons) - pad_deg,
            max(lats) + pad_deg, max(lons) + pad_deg)


def _parse_charger(poi: dict) -> Optional[Charger]:
    ai = poi.get("AddressInfo") or {}
    lat, lon = ai.get("Latitude"), ai.get("Longitude")
    if lat is None or lon is None:
        return None

    conns = poi.get("Connections") or []
    powers = [c.get("PowerKW") for c in conns if c.get("PowerKW")]
    max_power = max(powers) if powers else 0.0
    num_points = sum((c.get("Quantity") or 1) for c in conns) if conns else 1
    connector_types = sorted({
        (c.get("ConnectionType") or {}).get("Title", "")
        for c in conns if (c.get("ConnectionType") or {}).get("Title")
    })

    status = poi.get("StatusType") or {}
    is_op = status.get("IsOperational")  # True / False / None

    return Charger(
        id=poi.get("ID", 0),
        name=ai.get("Title", "Unknown site"),
        lat=float(lat), lon=float(lon),
        address=", ".join(filter(None, [ai.get("AddressLine1"), ai.get("Postcode")])),
        town=ai.get("Town") or "",
        max_power_kw=float(max_power),
        num_points=int(num_points),
        connectors=connector_types,
        is_operational=is_op,
        status_title=status.get("Title", "Unknown"),
    )


def fetch_chargers_in_bbox(bbox, api_key: str, max_results: int = 2000,
                           country_code: str = "") -> list[Charger]:
    """
    Single OCM call for a bounding box. OCM's boundingbox param wants
    (lat1,lon1),(lat2,lon2).

    Raises ChargerAPIError with a user-readable message on failure — OCM
    requires an API key, and a silent empty result here would masquerade
    as "no chargers exist along this route".
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    params = {
        "output": "json",
        "boundingbox": f"({min_lat},{min_lon}),({max_lat},{max_lon})",
        "maxresults": max_results,
        "compact": "true",
        "verbose": "false",
    }
    if api_key:
        params["key"] = api_key
    if country_code:
        params["countrycode"] = country_code
    try:
        resp = requests.get(OCM_URL, params=params,
                            headers={"User-Agent": USER_AGENT},
                            timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise ChargerAPIError(f"Couldn't reach Open Charge Map: {exc}") from exc
    if resp.status_code != 200:
        # OCM returns plain-text reasons, e.g. "You must specify an API key"
        # or "Invalid API key." — pass them through.
        detail = resp.text.strip()[:200] or f"HTTP {resp.status_code}"
        raise ChargerAPIError(f"Open Charge Map rejected the request: {detail}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ChargerAPIError("Open Charge Map returned an unreadable "
                              "response.") from exc
    if not isinstance(data, list):
        raise ChargerAPIError(f"Open Charge Map returned an error: "
                              f"{str(data)[:200]}")
    out = []
    for poi in data:
        c = _parse_charger(poi)
        if c:
            out.append(c)
    return out


# --------------------------------------------------------------------------- #
# Wait-time model
# --------------------------------------------------------------------------- #

def _busyness_factor(hour: int) -> float:
    """
    Rough utilisation curve (0..1) by hour of day. Twin peaks at the
    morning and evening commute, quiet overnight.
    """
    curve = {
        0: .05, 1: .04, 2: .03, 3: .03, 4: .05, 5: .12, 6: .30,
        7: .55, 8: .60, 9: .45, 10: .40, 11: .45, 12: .55, 13: .50,
        14: .42, 15: .48, 16: .62, 17: .70, 18: .65, 19: .48, 20: .35,
        21: .25, 22: .15, 23: .08,
    }
    return curve.get(hour % 24, 0.3)


def _session_minutes(max_power_kw: float) -> int:
    """Typical time a car occupies a bay, by charger speed."""
    if max_power_kw >= 150:
        return 20
    if max_power_kw >= 50:
        return 35
    if max_power_kw >= 22:
        return 80
    return 150


def estimate_wait(charger: Charger, when: Optional[datetime] = None) -> None:
    """
    Populate wait fields on the charger.

    Live status (from OCM) is used when it tells us the site is not
    operational. OCM does not expose real-time bay occupancy on the free
    API, so expected wait is modelled from power, bay count and time of day
    and clearly labelled as an estimate.
    """
    when = when or datetime.now()
    hour = when.hour

    # Live status short-circuit.
    if charger.is_operational is False:
        charger.wait_band = "Out of service"
        charger.busyness_pct = 100
        charger.wait_minutes_est = None
        charger.wait_source = "live"
        charger.charge_time_min = 0
        charger.bays_open_est = 0
        return

    util = _busyness_factor(hour)
    session = _session_minutes(charger.max_power_kw)
    bays = max(1, charger.num_points)

    # More bays absorb demand, so per-bay utilisation falls as bays rise.
    # Divisor keeps a single bay near the raw demand and damps multi-bay sites.
    per_bay_util = min(0.90, util / (0.7 + 0.5 * bays))
    charger.busyness_pct = int(round(per_bay_util * 100))

    # Expected bays occupied right now = per-bay utilisation across all bays.
    # Modelled, not measured — OCM's free API has no live occupancy.
    in_use = min(bays, int(round(per_bay_util * bays)))
    charger.bays_open_est = bays - in_use

    # Simple queueing-style expected wait: rises steeply as utilisation -> 1,
    # capped so a prototype estimate never returns an absurd number.
    expected = session * (per_bay_util ** 2) / max(0.10, 1 - per_bay_util) / bays
    expected = min(45, max(0, round(expected)))
    charger.wait_minutes_est = int(expected)

    if expected < 3:
        charger.wait_band = "No wait"
    elif expected < 10:
        charger.wait_band = "Short (<10 min)"
    elif expected < 25:
        charger.wait_band = "Moderate (10-25 min)"
    else:
        charger.wait_band = "Busy (25 min+)"

    # If OCM explicitly says operational, note that the operational check is
    # live even though the wait number is modelled.
    charger.wait_source = "live+modelled" if charger.is_operational is True else "modelled"

    # A representative charge session for this bay's power (for trip totals).
    charger.charge_time_min = session


# --------------------------------------------------------------------------- #
# Charge-stop planning
# --------------------------------------------------------------------------- #

def _dedupe(chargers: list[Charger]) -> list[Charger]:
    seen = set()
    out = []
    for c in chargers:
        if c.id in seen:
            continue
        seen.add(c.id)
        out.append(c)
    return out


def chargers_along_route(route: Route, api_key: str,
                         corridor_km: float = 8.0,
                         min_power_kw: float = 0.0) -> list[Charger]:
    """
    All chargers within `corridor_km` of the route polyline, annotated with
    distance-along-route and wait estimates, sorted by distance along route.
    """
    raw = fetch_chargers_in_bbox(_bbox(route.coords), api_key)
    raw = _dedupe(raw)
    cum = _cumulative_distances(route.coords)

    kept = []
    for c in raw:
        d, along = _min_dist_to_route_km((c.lat, c.lon), route.coords, cum)
        if d <= corridor_km and c.max_power_kw >= min_power_kw:
            c.dist_along_km = along
            c.off_route_km = d
            estimate_wait(c)
            kept.append(c)
    kept.sort(key=lambda x: x.dist_along_km)
    return kept


def plan_stops(route: Route, corridor_chargers: list[Charger],
               range_km: float, start_soc: float = 0.9,
               reserve: float = 0.15, min_power_kw: float = 50.0) -> list[Charger]:
    """
    Greedy charge-stop selection.

    Starting with `start_soc` of range and never dropping below `reserve`,
    pick the furthest reachable fast charger (>= min_power_kw) before range
    runs out, "recharge", and repeat until the destination is reachable.
    """
    usable_first = range_km * (start_soc - reserve)
    usable_full = range_km * (1 - reserve)

    fast = [c for c in corridor_chargers if c.max_power_kw >= min_power_kw
            and c.is_operational is not False]
    fast.sort(key=lambda x: x.dist_along_km)

    stops: list[Charger] = []
    pos = 0.0
    reach = usable_first
    total = route.distance_km

    while reach < total:
        # Furthest charger we can still reach from current position.
        candidates = [c for c in fast
                      if pos < c.dist_along_km <= reach and c not in stops]
        if not candidates:
            break  # can't make it — gap too large for available fast chargers
        nxt = max(candidates, key=lambda x: x.dist_along_km)
        stops.append(nxt)
        pos = nxt.dist_along_km
        reach = pos + usable_full
    return stops


def estimate_charge_times(stops: list[Charger], range_km: float,
                          battery_kwh: float, start_soc: float = 0.9,
                          taper: float = 0.75) -> None:
    """
    Set charge_time_min on each planned stop from the energy actually needed.

    Simulates state-of-charge along the trip: arrive at each stop having spent
    leg_km / range_km of battery, charge back to full (matching the planner's
    assumption), at the charger's power damped by an average taper factor.
    This replaces the flat per-speed session time, which ignored how empty
    the car arrives.
    """
    soc = start_soc
    prev_km = 0.0
    for s in sorted(stops, key=lambda x: x.dist_along_km):
        leg_km = s.dist_along_km - prev_km
        soc_arrive = max(0.0, soc - leg_km / range_km)
        energy_kwh = (1.0 - soc_arrive) * battery_kwh
        avg_power_kw = max(10.0, s.max_power_kw) * taper
        s.charge_time_min = int(round(energy_kwh / avg_power_kw * 60))
        soc = 1.0
        prev_km = s.dist_along_km


# --------------------------------------------------------------------------- #
# Trip summary
# --------------------------------------------------------------------------- #

@dataclass
class TripPlan:
    route: Route
    stops: list[Charger]
    corridor: list[Charger]
    reachable: bool
    total_charge_min: int = 0
    total_wait_min: int = 0
    charger_error: str = ""            # non-empty if the OCM fetch failed

    @property
    def total_trip_min(self) -> float:
        return self.route.duration_min + self.total_charge_min + self.total_wait_min


def build_trip(route: Route, api_key: str, range_km: float,
               start_soc: float = 0.9, corridor_km: float = 8.0,
               min_power_kw: float = 50.0,
               battery_kwh: Optional[float] = None) -> TripPlan:
    # A failed charger fetch shouldn't kill the whole plan — short trips are
    # still valid without charger data, so record the error and carry on.
    charger_error = ""
    try:
        corridor = chargers_along_route(route, api_key, corridor_km,
                                        min_power_kw=0.0)
    except ChargerAPIError as exc:
        corridor = []
        charger_error = str(exc)
    stops = plan_stops(route, corridor, range_km, start_soc=start_soc,
                       min_power_kw=min_power_kw)
    # Charge times from energy needed; ~0.18 kWh/km if no battery size given.
    if battery_kwh is None:
        battery_kwh = range_km * 0.18
    estimate_charge_times(stops, range_km, battery_kwh, start_soc=start_soc)
    reachable = (range_km * (start_soc - 0.15) >= route.distance_km) or (
        len(stops) > 0 and _covers(route.distance_km, stops, range_km, start_soc))
    total_charge = sum(c.charge_time_min for c in stops)
    total_wait = sum(c.wait_minutes_est or 0 for c in stops)
    return TripPlan(route=route, stops=stops, corridor=corridor,
                    reachable=reachable, total_charge_min=total_charge,
                    total_wait_min=total_wait, charger_error=charger_error)


def _covers(total_km: float, stops: list[Charger], range_km: float,
            start_soc: float, reserve: float = 0.15) -> bool:
    """Check the chosen stops actually bridge start -> destination."""
    pos = 0.0
    reach = range_km * (start_soc - reserve)
    for s in sorted(stops, key=lambda x: x.dist_along_km):
        if s.dist_along_km > reach:
            return False
        pos = s.dist_along_km
        reach = pos + range_km * (1 - reserve)
    return reach >= total_km
