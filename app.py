"""
EV Route Planner — Streamlit app.

Plan a drive between two places, see the driving route on a map, and get
fast-charging stops placed along it based on your EV's range, with live
station status (where Open Charge Map provides it) and modelled wait-time
estimates.

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
# Sidebar — inputs
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.title("⚡ EV Route Planner")
    st.caption("Charging stops & wait estimates along your trip.")

    start_q = st.text_input("Start", value="Cape Town, South Africa")
    end_q = st.text_input("Destination", value="Bloemfontein, South Africa")

    st.subheader("Your EV")
    range_km = st.slider("Full range (km)", 150, 700, 350, step=10,
                         help="Rated range on a full charge.")
    start_soc = st.slider("Starting charge (%)", 30, 100, 90, step=5) / 100.0

    st.subheader("Charging preferences")
    min_power = st.select_slider(
        "Minimum charger speed for stops (kW)",
        options=[22, 50, 100, 150, 350], value=50,
        help="Only use chargers at or above this power for planned stops.")
    corridor_km = st.slider("Search corridor around route (km)", 2, 25, 8,
                            help="How far off the route to look for chargers.")

    st.subheader("Open Charge Map")
    api_key = st.text_input(
        "API key", type="password",
        help="Free key from openchargemap.org/site/develop. Works without "
             "one but is rate-limited.")

    go = st.button("Plan trip", type="primary", use_container_width=True)

    st.divider()
    st.caption("Data: OpenStreetMap (geocoding), OSRM (routing), "
               "Open Charge Map (chargers). Wait times are estimates.")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def fmt_dur(minutes: float) -> str:
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


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


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

st.markdown("## Trip plan")

if not go:
    st.info("Set your start, destination and EV range in the sidebar, then "
            "press **Plan trip**.")
    st.stop()

with st.status("Planning your trip…", expanded=True) as status:
    st.write("Geocoding start & destination…")
    start = core.geocode(start_q)
    end = core.geocode(end_q)
    if not start or not end:
        status.update(label="Couldn't find one of the places.", state="error")
        st.error("Geocoding failed. Try a more specific place name.")
        st.stop()

    st.write("Fetching driving route…")
    route = core.get_route(start, end)
    if not route:
        status.update(label="Routing failed.", state="error")
        st.error("Couldn't compute a driving route between these points.")
        st.stop()

    st.write("Finding chargers & estimating waits…")
    trip = core.build_trip(route, api_key, range_km, start_soc=start_soc,
                           corridor_km=corridor_km, min_power_kw=min_power)
    status.update(label="Trip planned.", state="complete")


# --- summary metrics ------------------------------------------------------- #

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Distance", f"{route.distance_km:,.0f} km")
c2.metric("Drive time", fmt_dur(route.duration_min))
c3.metric("Charge stops", len(trip.stops))
c4.metric("Charging + wait", fmt_dur(trip.total_charge_min + trip.total_wait_min))
c5.metric("Total trip", fmt_dur(trip.total_trip_min))

if not trip.reachable and not trip.stops:
    st.warning("With this range and starting charge you can't complete the "
               "trip on the available fast chargers along the corridor. Try a "
               "wider search corridor, a lower minimum charger speed, or a "
               "higher starting charge.")
elif trip.stops:
    st.success(f"Planned {len(trip.stops)} charging stop(s) along the route.")
else:
    st.success("No charging stop needed — this trip is within your range.")

# --- map ------------------------------------------------------------------- #

mid_lat = (start.lat + end.lat) / 2
mid_lon = (start.lon + end.lon) / 2
m = folium.Map(location=[mid_lat, mid_lon], zoom_start=6, tiles="cartodbpositron")

AntPath(locations=route.coords, color="#2563eb", weight=5, delay=1000).add_to(m)

folium.Marker([start.lat, start.lon], tooltip="Start",
              icon=folium.Icon(color="green", icon="play", prefix="fa")).add_to(m)
folium.Marker([end.lat, end.lon], tooltip="Destination",
              icon=folium.Icon(color="red", icon="flag-checkered", prefix="fa")).add_to(m)

# All corridor chargers as light dots.
for c in trip.corridor:
    folium.CircleMarker(
        [c.lat, c.lon], radius=3, color=power_color(c.max_power_kw),
        fill=True, fill_opacity=0.5, opacity=0.5,
        tooltip=f"{c.name} · {c.max_power_kw:.0f} kW",
    ).add_to(m)

# Planned stops as numbered, prominent markers.
stop_ids = {s.id for s in trip.stops}
for i, s in enumerate(trip.stops, 1):
    popup = folium.Popup(html=(
        f"<b>Stop {i}: {s.name}</b><br>"
        f"{s.town}<br>"
        f"{s.max_power_kw:.0f} kW · {s.num_points} bay(s)<br>"
        f"~{s.dist_along_km:.0f} km along route<br>"
        f"Status: {s.status_title}<br>"
        f"Wait: {s.wait_band} ({s.wait_source})"
    ), max_width=260)
    folium.Marker(
        [s.lat, s.lon], popup=popup, tooltip=f"Stop {i}: {s.name}",
        icon=folium.Icon(color="blue", icon=str(i) if i < 10 else "bolt",
                         prefix="fa"),
    ).add_to(m)

st_folium(m, use_container_width=True, height=520, returned_objects=[])

# --- stop details ---------------------------------------------------------- #

if trip.stops:
    st.markdown("### Charging stops")
    for i, s in enumerate(trip.stops, 1):
        with st.container(border=True):
            a, b, c, d = st.columns([3, 1.4, 1.4, 1.6])
            a.markdown(f"**{i}. {s.name}**  \n{s.town or s.address or '—'}")
            b.markdown(f"**{s.max_power_kw:.0f} kW**  \n{s.num_points} bay(s)")
            c.markdown(f"**{s.dist_along_km:.0f} km**  \nalong route")
            badge = wait_color(s.wait_band)
            wait_txt = (f"~{s.wait_minutes_est} min"
                        if s.wait_minutes_est is not None else s.wait_band)
            d.markdown(
                f"<span style='background:{badge};color:white;padding:2px 8px;"
                f"border-radius:10px;font-size:0.85em'>{s.wait_band}</span>"
                f"<br><small>{wait_txt} · {s.wait_source} · busy "
                f"{s.busyness_pct}%</small>",
                unsafe_allow_html=True)

# --- corridor table -------------------------------------------------------- #

with st.expander(f"All {len(trip.corridor)} chargers near the route"):
    rows = [{
        "Name": c.name,
        "Town": c.town,
        "Power (kW)": round(c.max_power_kw),
        "Bays": c.num_points,
        "Km along": round(c.dist_along_km),
        "Status": c.status_title,
        "Est. wait": (f"{c.wait_minutes_est} min"
                      if c.wait_minutes_est is not None else c.wait_band),
        "Source": c.wait_source,
    } for c in trip.corridor]
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.write("No chargers found in the corridor. Widen the search or add "
                 "an Open Charge Map API key.")

st.caption(f"Planned at {datetime.now():%Y-%m-%d %H:%M}. Wait times are "
           "modelled estimates (power, bay count, time-of-day); live station "
           "operational status is used where Open Charge Map provides it.")
