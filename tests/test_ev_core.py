"""Unit tests for ev_core — pure logic plus mocked API-failure paths.

Run with:  pytest
No network access required.
"""

from datetime import datetime

import pytest
import requests

import ev_core as core


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

ROUTE_1000 = core.Route(coords=[(-33.93, 18.42), (-29.12, 26.22)],
                        distance_km=1001.0, duration_min=600.0)


def make_charger(id_: int, along_km: float, kw: float = 150,
                 bays: int = 4, operational=True) -> core.Charger:
    c = core.Charger(id=id_, name=f"C{id_}", lat=0.0, lon=0.0, address="",
                     town="", max_power_kw=kw, num_points=bays,
                     connectors=[], is_operational=operational,
                     status_title="Operational")
    c.dist_along_km = along_km
    core.estimate_wait(c)
    return c


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def test_haversine_known_distance():
    cape_town = (-33.9249, 18.4241)
    joburg = (-26.2041, 28.0473)
    d = core.haversine_km(cape_town, joburg)
    assert 1250 < d < 1300  # great-circle ~1,270 km


def test_haversine_zero():
    assert core.haversine_km((0, 0), (0, 0)) == 0


# --------------------------------------------------------------------------- #
# Stop planning
# --------------------------------------------------------------------------- #

def test_plan_bridges_long_trip():
    chargers = [make_charger(i, km) for i, km in
                enumerate([200, 400, 600, 800], start=1)]
    stops = core.plan_stops(ROUTE_1000, chargers, range_km=350, start_soc=0.9)
    assert core._covers(1001.0, stops, 350, 0.9)


def test_out_of_service_charger_skipped():
    chargers = [make_charger(1, 200, operational=False),
                make_charger(2, 250), make_charger(3, 500),
                make_charger(4, 780)]
    stops = core.plan_stops(ROUTE_1000, chargers, range_km=350, start_soc=0.9)
    assert all(s.id != 1 for s in stops)
    assert core._covers(1001.0, stops, 350, 0.9)


def test_unbridgeable_gap_returns_partial_not_crash():
    chargers = [make_charger(1, 200), make_charger(2, 900)]  # 700 km gap
    stops = core.plan_stops(ROUTE_1000, chargers, range_km=350, start_soc=0.9)
    assert not core._covers(1001.0, stops, 350, 0.9)


def test_min_power_filter():
    chargers = [make_charger(1, 200, kw=22), make_charger(2, 220, kw=150)]
    stops = core.plan_stops(ROUTE_1000, chargers, range_km=350,
                            start_soc=0.9, min_power_kw=50)
    assert all(s.max_power_kw >= 50 for s in stops)


def test_short_trip_needs_no_stops():
    short = core.Route(coords=[(0, 0), (0.4, 0)], distance_km=45.0,
                       duration_min=40.0)
    stops = core.plan_stops(short, [make_charger(1, 20)], range_km=350,
                            start_soc=0.9)
    assert stops == []


# --------------------------------------------------------------------------- #
# Wait model & bays open
# --------------------------------------------------------------------------- #

def test_out_of_service_is_live_zero_bays():
    c = make_charger(1, 100, operational=False)
    assert c.wait_band == "Out of service"
    assert c.wait_source == "live"
    assert c.bays_open_est == 0


def test_bays_open_invariant_all_hours():
    for hour in range(24):
        for bays in (1, 2, 4, 8, 12):
            c = core.Charger(id=1, name="X", lat=0, lon=0, address="",
                             town="", max_power_kw=150, num_points=bays,
                             connectors=[], is_operational=True,
                             status_title="")
            core.estimate_wait(c, when=datetime(2026, 7, 9, hour))
            assert 0 <= c.bays_open_est <= bays


def test_quiet_hours_more_open_than_peak():
    def open_at(hour):
        c = core.Charger(id=1, name="X", lat=0, lon=0, address="", town="",
                         max_power_kw=150, num_points=4, connectors=[],
                         is_operational=True, status_title="")
        core.estimate_wait(c, when=datetime(2026, 7, 9, hour))
        return c.bays_open_est
    assert open_at(3) >= open_at(17)


# --------------------------------------------------------------------------- #
# Charge-time model
# --------------------------------------------------------------------------- #

def test_charge_time_scales_with_energy_needed():
    # Arriving emptier (longer first leg) must mean a longer charge.
    near = make_charger(1, 100)
    far = make_charger(2, 250)
    core.estimate_charge_times([near], 350, battery_kwh=64, start_soc=0.9)
    core.estimate_charge_times([far], 350, battery_kwh=64, start_soc=0.9)
    assert far.charge_time_min > near.charge_time_min


def test_faster_charger_charges_quicker():
    slow = make_charger(1, 200, kw=50)
    fast = make_charger(2, 200, kw=150)
    core.estimate_charge_times([slow], 350, battery_kwh=64, start_soc=0.9)
    core.estimate_charge_times([fast], 350, battery_kwh=64, start_soc=0.9)
    assert fast.charge_time_min < slow.charge_time_min


def test_bigger_battery_takes_longer():
    a = make_charger(1, 200)
    b = make_charger(2, 200)
    core.estimate_charge_times([a], 350, battery_kwh=40, start_soc=0.9)
    core.estimate_charge_times([b], 350, battery_kwh=100, start_soc=0.9)
    assert b.charge_time_min > a.charge_time_min


def test_charge_times_simulate_soc_across_stops():
    stops = [make_charger(1, 200), make_charger(2, 450)]
    core.estimate_charge_times(stops, 350, battery_kwh=64, start_soc=0.9)
    assert all(s.charge_time_min > 0 for s in stops)


# --------------------------------------------------------------------------- #
# OCM fetch error handling (mocked — no network)
# --------------------------------------------------------------------------- #

def test_fetch_403_raises_readable_error(monkeypatch):
    monkeypatch.setattr(core.requests, "get", lambda *a, **k: FakeResponse(
        status_code=403, text="You must specify an API key using the key "
                              "query parameter or x-api-key header."))
    with pytest.raises(core.ChargerAPIError, match="API key"):
        core.fetch_chargers_in_bbox((0, 0, 1, 1), api_key="")


def test_fetch_network_error_raises(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("dns failure")
    monkeypatch.setattr(core.requests, "get", boom)
    with pytest.raises(core.ChargerAPIError, match="Couldn't reach"):
        core.fetch_chargers_in_bbox((0, 0, 1, 1), api_key="k")


def test_fetch_non_list_json_raises(monkeypatch):
    monkeypatch.setattr(core.requests, "get", lambda *a, **k: FakeResponse(
        status_code=200, json_data={"status": "error"}))
    with pytest.raises(core.ChargerAPIError):
        core.fetch_chargers_in_bbox((0, 0, 1, 1), api_key="k")


def test_build_trip_carries_charger_error(monkeypatch):
    monkeypatch.setattr(core.requests, "get", lambda *a, **k: FakeResponse(
        status_code=403, text="Invalid API key."))
    trip = core.build_trip(ROUTE_1000, api_key="bad", range_km=350)
    assert trip.charger_error
    assert trip.stops == []


def test_build_trip_short_trip_reachable_despite_fetch_failure(monkeypatch):
    monkeypatch.setattr(core.requests, "get", lambda *a, **k: FakeResponse(
        status_code=403, text="Invalid API key."))
    short = core.Route(coords=[(0, 0), (0.4, 0)], distance_km=45.0,
                       duration_min=40.0)
    trip = core.build_trip(short, api_key="bad", range_km=350, start_soc=0.9)
    assert trip.reachable
