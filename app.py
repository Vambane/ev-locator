"""
EV Route Planner — Streamlit app.

Two modes, in the style of charging-network apps like Electrify America:

  * Find a charger — search stations near a place, see availability,
    speed tiers, wait estimates and directions.
  * Plan a trip — drive between two places with fast-charging stops
    placed along the route based on your EV's range.

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime

import folium
import streamlit as st
from folium.plugins import AntPath
from streamlit_folium import st_folium

import ev_core as core

st.set_page_config(page_title="EV Route Planner", page_icon="⚡", layout="wide")

# --------------------------------------------------------------------------- #
# Cached data access — identical inputs never re-hit the free public APIs.
# --------------------------------------------------------------------------- #


@st.cache_data(ttl=3600, show_spinner=False)
def find_places(query: str) -> list[core.Place]:
    """Geocode candidates, cached for an hour per query string."""
    return core.geocode_candidates(query)


@st.cache_data(ttl=3600, show_spinner=False)
def find_route(s_lat: float, s_lon: float,
               e_lat: float, e_lon: float) -> core.Route | None:
    """Driving route between two coordinate pairs, cached for an hour."""
    return core.get_route(core.Place("start", s_lat, s_lon),
                          core.Place("end", e_lat, e_lon))


@st.cache_data(ttl=300, show_spinner=False)
def plan_trip(route: core.Route, api_key: str, range_km: float,
              start_soc: float, corridor_km: float, min_power: float,
              battery_kwh: float) -> core.TripPlan:
    """Full trip plan. Short TTL: wait estimates are time-of-day dependent."""
    return core.build_trip(route, api_key, range_km, start_soc=start_soc,
                           corridor_km=corridor_km, min_power_kw=min_power,
                           battery_kwh=battery_kwh)


@st.cache_data(ttl=120, show_spinner=False)
def chargers_near(lat: float, lon: float, radius_km: float,
                  api_key: str) -> list[core.Charger]:
    """Nearby-station search. Short TTL keeps availability estimates fresh."""
    return core.fetch_chargers_near(lat, lon, radius_km, api_key)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def fmt_dur(minutes: float) -> str:
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def short_place(p: core.Place) -> str:
    """Compact display name: first parts + country."""
    parts = p.name.split(", ")
    if len(parts) <= 4:
        return p.name
    return ", ".join(parts[:3]) + ", " + parts[-1]


def power_color(kw: float) -> str:
    if kw >= 150:
        return "#7c3aed"   # ultra-rapid
    if kw >= 50:
        return "#16a34a"   # rapid
    if kw >= 22:
        return "#ea580c"   # fast
    return "#6b7280"       # slow


def wait_color(band: str) -> str:
    return {
        "No wait": "#16a34a",
        "Short (<10 min)": "#65a30d",
        "Moderate (10-25 min)": "#ea580c",
        "Busy (25 min+)": "#dc2626",
        "Out of service": "#6b7280",
    }.get(band, "#6b7280")


def availability_marker_color(c: core.Charger) -> str:
    """Folium marker colour by availability, like a network app's pins."""
    if c.is_operational is False:
        return "lightgray"
    if c.bays_open_est == 0:
        return "orange"
    return "green"


def badge(text: str, color: str) -> str:
    return (f"<span style='background:{color};color:white;padding:2px 8px;"
            f"border-radius:10px;font-size:0.85em'>{text}</span>")


# --------------------------------------------------------------------------- #
# Sidebar — branding + API key only (each tab carries its own inputs)
# --------------------------------------------------------------------------- #

# The key stays server-side: when configured in secrets it is used directly
# and never rendered into a widget, so visitors can't see it.
try:
    secret_key = st.secrets.get("ocm_api_key", "")
except (FileNotFoundError, st.errors.StreamlitAPIException):
    secret_key = ""

with st.sidebar:
    st.title("⚡ EV Route Planner")
    st.caption("Find chargers near you, or plan a road trip with "
               "charging stops.")

    if secret_key:
        api_key = secret_key
    else:
        st.subheader("Open Charge Map")
        api_key = st.text_input(
            "API key", type="password",
            help="Required — get a free key at "
                 "openchargemap.org/site/develop, or add it to "
                 ".streamlit/secrets.toml as ocm_api_key.")
        if not api_key:
            st.warning("Open Charge Map now requires an API key — without "
                       "one, no chargers can be found.", icon="🔑")

    st.divider()
    st.caption("Data: OpenStreetMap (geocoding), OSRM (routing), "
               "Open Charge Map (chargers). Wait times are estimates.")


tab_find, tab_plan = st.tabs(["🔌 Find a charger", "🗺️ Plan a trip"])

# =========================================================================== #
# Tab 1 — Find a charger (station locator)
# =========================================================================== #

with tab_find:
    with st.form("find_form"):
        f1, f2, f3, f4 = st.columns([3, 1.2, 1.4, 1.4])
        near_q = f1.text_input("Near", value="Cape Town, South Africa",
                               help="Address, suburb or city to search around.")
        radius_km = f2.slider("Radius (km)", 5, 100, 25)
        find_min_kw = f3.select_slider("Min speed (kW)",
                                       options=[0, 22, 50, 100, 150, 350],
                                       value=0)
        hide_busy = f4.checkbox("Only likely available", value=False,
                                help="Hide out-of-service stations and those "
                                     "with no bays likely open.")
        find_go = st.form_submit_button("Search", type="primary")

    if find_go:
        st.session_state.searched = True

    if not st.session_state.get("searched"):
        st.info("Search for charging stations near an address or city.")
    else:
        near_opts = find_places(near_q)
        if not near_opts:
            st.error("Couldn't find that location. Try a more specific "
                     "place name (e.g. add the country).")
            st.stop()
        centre = st.selectbox("Location", near_opts, format_func=short_place,
                              key=f"sel_near::{near_q}",
                              label_visibility="collapsed")

        with st.spinner("Finding chargers…"):
            try:
                stations = chargers_near(centre.lat, centre.lon,
                                         float(radius_km), api_key)
            except core.ChargerAPIError as exc:
                st.error(f"**Charger lookup failed** — {exc}\n\n"
                         "Add a free Open Charge Map API key in the sidebar "
                         "(https://openchargemap.org/site/develop).")
                st.stop()

        # Apply the user's filters after the (cached) fetch.
        shown = [c for c in stations if c.max_power_kw >= find_min_kw]
        if hide_busy:
            shown = [c for c in shown
                     if c.is_operational is not False and c.bays_open_est != 0]

        n_avail = sum(1 for c in shown
                      if c.is_operational is not False and c.bays_open_est)
        m1, m2, m3 = st.columns(3)
        m1.metric("Stations found", len(shown))
        m2.metric("Likely available now", n_avail)
        m3.metric("Search radius", f"{radius_km} km")

        if not shown:
            st.warning("No stations match. Widen the radius or relax the "
                       "filters.")
        else:
            # --- map ------------------------------------------------------- #
            fm = folium.Map(tiles="cartodbpositron")
            pts = [(c.lat, c.lon) for c in shown] + [(centre.lat, centre.lon)]
            fm.fit_bounds([[min(p[0] for p in pts), min(p[1] for p in pts)],
                           [max(p[0] for p in pts), max(p[1] for p in pts)]])
            folium.Marker([centre.lat, centre.lon], tooltip="Search centre",
                          icon=folium.Icon(color="blue", icon="user",
                                           prefix="fa")).add_to(fm)
            for c in shown:
                popup = folium.Popup(html=(
                    f"<b>{c.name}</b><br>{c.town}<br>"
                    f"{core.power_tier(c.max_power_kw)}<br>"
                    f"{core.availability_label(c)}<br>"
                    f"Wait: {c.wait_band} ({c.wait_source})<br>"
                    f"<a href='{core.directions_link(c.lat, c.lon)}' "
                    f"target='_blank'>Directions</a>"
                ), max_width=260)
                folium.Marker(
                    [c.lat, c.lon], popup=popup, tooltip=c.name,
                    icon=folium.Icon(color=availability_marker_color(c),
                                     icon="bolt", prefix="fa"),
                ).add_to(fm)
            st_folium(fm, use_container_width=True, height=440,
                      returned_objects=[], key="find_map")

            # --- station list, nearest first ------------------------------- #
            st.markdown(f"### {len(shown)} station(s), nearest first")
            for c in shown[:50]:
                with st.container(border=True):
                    a, b, cc, d = st.columns([3, 1.8, 1.6, 1.4])
                    a.markdown(f"**{c.name}**  \n"
                               f"{c.town or c.address or '—'} · "
                               f"{c.dist_from_search_km:.1f} km away")
                    a.markdown(
                        f"[Directions ↗]({core.directions_link(c.lat, c.lon)})")
                    b.markdown(badge(core.power_tier(c.max_power_kw),
                                     power_color(c.max_power_kw)),
                               unsafe_allow_html=True)
                    if c.connectors:
                        b.markdown(f"<small>{' · '.join(c.connectors)}</small>",
                                   unsafe_allow_html=True)
                    avail = core.availability_label(c)
                    avail_color = ("#6b7280" if c.is_operational is False
                                   else "#dc2626" if c.bays_open_est == 0
                                   else "#16a34a")
                    cc.markdown(badge(avail, avail_color),
                                unsafe_allow_html=True)
                    wait_txt = (f"~{c.wait_minutes_est} min wait"
                                if c.wait_minutes_est is not None
                                else c.wait_band)
                    d.markdown(f"<small>{wait_txt}<br>{c.wait_source} · busy "
                               f"{c.busyness_pct}%</small>",
                               unsafe_allow_html=True)
            if len(shown) > 50:
                st.caption(f"Showing the nearest 50 of {len(shown)} stations.")

# =========================================================================== #
# Tab 2 — Plan a trip
# =========================================================================== #

with tab_plan:
    with st.form("trip_form"):
        t1, t2 = st.columns(2)
        start_q = t1.text_input("Start", value="Cape Town, South Africa")
        end_q = t2.text_input("Destination",
                              value="Bloemfontein, South Africa")

        e1, e2, e3 = st.columns(3)
        range_km = e1.slider("Full range (km)", 150, 700, 350, step=10,
                             help="Rated range on a full charge.")
        battery_kwh = e2.slider("Battery capacity (kWh)", 20, 150, 64,
                                help="Used to estimate charging time from "
                                     "the energy actually needed.")
        start_soc = e3.slider("Starting charge (%)", 30, 100, 90,
                              step=5) / 100.0

        p1, p2 = st.columns(2)
        min_power = p1.select_slider(
            "Minimum charger speed for stops (kW)",
            options=[22, 50, 100, 150, 350], value=50,
            help="Only use chargers at or above this power for planned stops.")
        corridor_km = p2.slider("Search corridor around route (km)", 2, 25, 8,
                                help="How far off the route to look for "
                                     "chargers.")

        go = st.form_submit_button("Plan trip", type="primary",
                                   use_container_width=True)

    # The form button is only True for one rerun; remember that a plan was
    # asked for so results survive later widget interactions.
    if go:
        st.session_state.planned = True

    if not st.session_state.get("planned"):
        st.info("Set your start, destination and EV range, then press "
                "**Plan trip**.")
        st.stop()

    # --- resolve locations (user-confirmable) ------------------------------ #

    with st.spinner("Looking up locations…"):
        start_opts = find_places(start_q)
        end_opts = find_places(end_q)

    if not start_opts or not end_opts:
        which = "start" if not start_opts else "destination"
        st.error(f"Couldn't find the {which} location. Try a more specific "
                 "place name (e.g. add the country).")
        st.stop()

    # Let the user confirm *which* match was meant, not trust the top hit.
    ambiguous = len(start_opts) > 1 or len(end_opts) > 1
    with st.expander("📍 Confirm locations", expanded=ambiguous):
        col_a, col_b = st.columns(2)
        start = col_a.selectbox("Start", start_opts, format_func=short_place,
                                key=f"sel_start::{start_q}")
        end = col_b.selectbox("Destination", end_opts,
                              format_func=short_place,
                              key=f"sel_end::{end_q}")

    # --- route + plan (cached, so reruns and repeat plans are instant) ----- #

    with st.spinner("Planning your trip…"):
        route = find_route(start.lat, start.lon, end.lat, end.lon)
        if not route:
            st.error("Couldn't compute a driving route between these points.")
            st.stop()
        trip = plan_trip(route, api_key, range_km, start_soc, corridor_km,
                         min_power, battery_kwh)

    # --- summary metrics ---------------------------------------------------- #

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Distance", f"{route.distance_km:,.0f} km")
    c2.metric("Drive time", fmt_dur(route.duration_min))
    c3.metric("Charge stops", len(trip.stops))
    c4.metric("Charging + wait",
              fmt_dur(trip.total_charge_min + trip.total_wait_min))
    c5.metric("Total trip", fmt_dur(trip.total_trip_min))

    if trip.charger_error:
        st.error(f"**Charger lookup failed** — {trip.charger_error}\n\n"
                 "Add a free Open Charge Map API key in the sidebar "
                 "(get one at https://openchargemap.org/site/develop) and "
                 "plan the trip again.")
    elif not trip.reachable and not trip.stops:
        st.warning("With this range and starting charge you can't complete "
                   "the trip on the available fast chargers along the "
                   "corridor. Try a wider search corridor, a lower minimum "
                   "charger speed, or a higher starting charge.")
    elif trip.stops:
        st.success(f"Planned {len(trip.stops)} charging stop(s) along the "
                   "route.")
    else:
        st.success("No charging stop needed — this trip is within your "
                   "range.")

    # --- map ----------------------------------------------------------------- #

    m = folium.Map(tiles="cartodbpositron")
    # Zoom to the actual route rather than a fixed level, so short and long
    # trips both fill the frame.
    lats = [c[0] for c in route.coords]
    lons = [c[1] for c in route.coords]
    m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

    AntPath(locations=route.coords, color="#2563eb", weight=5,
            delay=1000).add_to(m)

    folium.Marker([start.lat, start.lon], tooltip="Start",
                  icon=folium.Icon(color="green", icon="play",
                                   prefix="fa")).add_to(m)
    folium.Marker([end.lat, end.lon], tooltip="Destination",
                  icon=folium.Icon(color="red", icon="flag-checkered",
                                   prefix="fa")).add_to(m)

    # All corridor chargers as light dots.
    for c in trip.corridor:
        folium.CircleMarker(
            [c.lat, c.lon], radius=3, color=power_color(c.max_power_kw),
            fill=True, fill_opacity=0.5, opacity=0.5,
            tooltip=f"{c.name} · {c.max_power_kw:.0f} kW",
        ).add_to(m)

    # Planned stops as numbered, prominent markers.
    for i, s in enumerate(trip.stops, 1):
        popup = folium.Popup(html=(
            f"<b>Stop {i}: {s.name}</b><br>"
            f"{s.town}<br>"
            f"{core.power_tier(s.max_power_kw)} · {s.num_points} bay(s)<br>"
            f"Bays open: ~{s.bays_open_est} of {s.num_points} (est.)<br>"
            f"~{s.dist_along_km:.0f} km along route · "
            f"{s.off_route_km:.1f} km off route<br>"
            f"Charge ~{s.charge_time_min} min<br>"
            f"Status: {s.status_title}<br>"
            f"Wait: {s.wait_band} ({s.wait_source})<br>"
            f"<a href='{core.directions_link(s.lat, s.lon)}' "
            f"target='_blank'>Directions</a>"
        ), max_width=260)
        folium.Marker(
            [s.lat, s.lon], popup=popup, tooltip=f"Stop {i}: {s.name}",
            icon=folium.Icon(color="blue", icon=str(i) if i < 10 else "bolt",
                             prefix="fa"),
        ).add_to(m)

    st_folium(m, use_container_width=True, height=520, returned_objects=[],
              key="trip_map")

    # --- stop details --------------------------------------------------------- #

    if trip.stops:
        st.markdown("### Charging stops")
        for i, s in enumerate(trip.stops, 1):
            with st.container(border=True):
                a, b, c, d = st.columns([3, 1.6, 1.6, 1.6])
                a.markdown(f"**{i}. {s.name}**  \n"
                           f"{s.town or s.address or '—'}")
                a.markdown(
                    f"[Directions ↗]({core.directions_link(s.lat, s.lon)})")
                open_txt = (f"~{s.bays_open_est} of {s.num_points} open"
                            if s.bays_open_est is not None
                            else f"{s.num_points} bay(s)")
                b.markdown(badge(core.power_tier(s.max_power_kw),
                                 power_color(s.max_power_kw)),
                           unsafe_allow_html=True)
                b.markdown(f"<small>{open_txt}</small>",
                           unsafe_allow_html=True)
                c.markdown(f"**{s.dist_along_km:.0f} km** along  \n"
                           f"+{s.off_route_km:.1f} km off route · "
                           f"charge ~{s.charge_time_min} min")
                wait_txt = (f"~{s.wait_minutes_est} min"
                            if s.wait_minutes_est is not None else s.wait_band)
                d.markdown(
                    badge(s.wait_band, wait_color(s.wait_band)) +
                    f"<br><small>{wait_txt} · {s.wait_source} · busy "
                    f"{s.busyness_pct}%</small>",
                    unsafe_allow_html=True)

    # --- corridor table --------------------------------------------------------- #

    with st.expander(f"All {len(trip.corridor)} chargers near the route"):
        rows = [{
            "Name": c.name,
            "Town": c.town,
            "Power (kW)": round(c.max_power_kw),
            "Bays": c.num_points,
            "Open (est.)": ("—" if c.bays_open_est is None
                            else c.bays_open_est),
            "Km along": round(c.dist_along_km),
            "Km off route": round(c.off_route_km, 1),
            "Status": c.status_title,
            "Est. wait": (f"{c.wait_minutes_est} min"
                          if c.wait_minutes_est is not None else c.wait_band),
            "Source": c.wait_source,
        } for c in trip.corridor]
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.write("No chargers found in the corridor. Widen the search "
                     "or add an Open Charge Map API key.")

st.caption(f"Planned at {datetime.now():%Y-%m-%d %H:%M}. Wait times are "
           "modelled estimates (power, bay count, time-of-day); live station "
           "operational status is used where Open Charge Map provides it.")
