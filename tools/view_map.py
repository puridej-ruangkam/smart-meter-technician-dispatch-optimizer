"""Draw the routes on a street map as mapdata/route_map.html.

    python tools/view_map.py    redraw the map from the Daily Route sheet
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import sys
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dispatch import config as cfg                              # noqa: E402
from dispatch.config import DEFAULT_CONFIG                      # noqa: E402
from dispatch.road_graph import RoadGraph                       # noqa: E402

log = logging.getLogger("view_map")

TEAM_COLOURS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Draw the road map and today's routes")
    p.add_argument("--graph", type=Path, default=DEFAULT_CONFIG.routing.graph_path)
    p.add_argument("--out", type=Path, default=PROJECT_DIR / "mapdata" / "route_map.html")
    p.add_argument("--workbook", type=Path, default=cfg.WORKBOOK_PATH,
                   help="Workbook to read the published plan from")
    return p.parse_args(argv)


# Roughly "primary road and above", judged by edge speed.
MAJOR_ROAD_KMH = 45.0

#: How many road segments to draw in the background.
MAX_BACKGROUND_SEGMENTS = 12_000


def major_roads(graph, min_speed_kmh: float = MAJOR_ROAD_KMH,
                max_segments: int = MAX_BACKGROUND_SEGMENTS) -> list:
    """Main-road segments from the road graph, for a background that needs no internet.

    Returns ``[[[lat, lon], [lat, lon]], ...]``.
    """
    seconds = graph._seconds_csr.data
    metres = graph._meters_flat
    indptr, indices = graph._indptr, graph._indices

    speed = np.divide(
        metres, np.maximum(seconds, 1e-6), out=np.zeros_like(metres, dtype=float),
        where=seconds > 0,
    ) * 3.6
    keep = speed >= float(min_speed_kmh)

    sources = np.repeat(np.arange(len(indptr) - 1), np.diff(indptr))
    u, v = sources[keep], indices[keep]

    # Every road is stored twice, once per direction; draw each of them once.
    forward = u < v
    u, v = u[forward], v[forward]

    if len(u) > max_segments:
        step = len(u) // max_segments + 1
        u, v = u[::step], v[::step]

    lat, lon = graph.lat, graph.lon
    log.info(
        "Background: %s major-road segments (>= %.0f km/h) drawn from the graph",
        f"{len(u):,}", min_speed_kmh,
    )
    return [
        [[round(float(lat[a]), 5), round(float(lon[a]), 5)],
         [round(float(lat[b]), 5), round(float(lon[b]), 5)]]
        for a, b in zip(u, v)
    ]


def read_plan_from_sheet(workbook_path: Path):
    """Read the published plan from the Daily Route sheet, so the map shows what the teams were given."""
    import openpyxl
    import pandas as pd

    from dispatch.scheduler import OUTPUT_COLUMNS

    log.info("Reading the published plan from %r ...", cfg.OUTPUT_SHEET)
    book = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if cfg.OUTPUT_SHEET not in book.sheetnames:
            raise SystemExit(
                f"\nThe workbook has no {cfg.OUTPUT_SHEET!r} sheet yet.\n\n"
                f"Publish a plan first:\n"
                f"    python main.py\n"
            )
        rows = list(book[cfg.OUTPUT_SHEET].iter_rows(values_only=True))
    finally:
        book.close()

    if len(rows) < 2:
        raise SystemExit(f"The {cfg.OUTPUT_SHEET!r} sheet is empty.")

    plan = pd.DataFrame(rows[1:], columns=list(rows[0]))
    missing = [c for c in OUTPUT_COLUMNS if c not in plan.columns]
    if missing:
        raise SystemExit(
            f"The {cfg.OUTPUT_SHEET!r} sheet is missing columns {missing}. "
            f"Re-publish it with:  python main.py"
        )
    log.info("Plan read: %d jobs across %d teams",
             len(plan), plan["Team"].nunique())
    return plan


def route_lines(graph: RoadGraph, plan, depot, metric: str | None = None,
                depots: dict | None = None) -> list[dict]:
    """One entry per team and day: the streets it drives and its stops in order."""
    has_date = "Date" in plan.columns
    if has_date:
        import pandas as pd            # imported lazily, as elsewhere here

        # Excel returns datetimes; keep plain ISO dates.
        plan = plan.copy()
        plan["Date"] = (
            pd.to_datetime(plan["Date"], errors="coerce")
              .dt.strftime("%Y-%m-%d")
              .fillna(plan["Date"].astype(str))
        )
    group_cols = ["Date", "Team"] if has_date else ["Team"]

    out = []
    for key, group in plan.groupby(group_cols, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        day = str(key[0]) if has_date else ""
        team = key[-1]
        group = group.sort_values("Sequence")

        # The day's branch depots when the plan names them (workforce.xlsx).
        start = end = depot
        if depots and "Start Depot" in group.columns:
            start = depots.get(str(group["Start Depot"].iloc[0]), depot)
            end = depots.get(str(group.get("End Depot", group["Start Depot"]).iloc[0]), depot)
        # "-" in End Depot: that day the crews do not drive back.
        returns = str(group["End Depot"].iloc[0]).strip() != "-" \
            if "End Depot" in group.columns else True
        lats = np.concatenate(([start[0]], group["Latitude"].to_numpy()))
        lons = np.concatenate(([start[1]], group["Longitude"].to_numpy()))
        if returns:
            lats, lons = np.append(lats, end[0]), np.append(lons, end[1])

        nodes, _ = graph.snap(lats, lons, DEFAULT_CONFIG.routing.snap_max_km)

        line = []
        for a, b in zip(nodes[:-1], nodes[1:]):
            # Draw the same kind of route the plan was costed on.
            path = graph.path_nodes(
                int(a), int(b),
                weight=metric or DEFAULT_CONFIG.routing.path_metric,
            )
            if not path:                      # unreachable: fall back to a straight hop
                path = [int(a), int(b)]
            line.extend([[float(graph.lat[n]), float(graph.lon[n])] for n in path])

        out.append({
            "team": int(team),
            "date": day,
            "colour": TEAM_COLOURS[(int(team) - 1) % len(TEAM_COLOURS)],
            "line": line,
            "km": round(float(group["Estimated Distance (km)"].sum()), 1),
            # Addressed by column name, not tuple position -- positional access
            # silently picks up the wrong field if the column order ever moves.
            "stops": [
                {
                    "seq": int(row["Sequence"]),
                    "lat": float(row["Latitude"]),
                    "lon": float(row["Longitude"]),
                    "urgent": str(row.get("Billing State", "")) == cfg.BILLING_URGENT,
                    "label": (
                        (f"{day}<br>" if day else "")
                        + f"Team {int(team)} stop {int(row['Sequence'])}<br>"
                        f"Customer {row['Customer No.']}<br>"
                        f"Billing Day {row.get('Billing Day', '')} &middot; "
                        f"{row.get('Billing State', '')}<br>"
                        f"arrive {row['Estimated Arrival Time']} &middot; "
                        f"{row['Estimated Distance (km)']} km"
                    ),
                }
                for _, row in group.iterrows()
            ],
        })
    return out


# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<meta charset="utf-8"><title>Dispatch map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
 body{{font-family:system-ui,sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#1b1f24}}
 h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px}}
 .sub{{color:#5b6472;font-size:13px;margin-bottom:20px}}
 .card{{background:#fff;border:1px solid #e2e5ea;border-radius:10px;padding:16px;margin-bottom:20px}}
 #map{{height:620px;border-radius:8px}}
 .legend span{{display:inline-block;margin-right:16px;font-size:13px}}
 .legend button.team{{font:inherit;font-size:13px;border:0;background:none;color:inherit;
   padding:2px 6px;margin:0 10px 0 -6px;border-radius:6px;cursor:pointer}}
 .legend button.team:hover,table.week th[data-team]:hover{{background:#eef1f4}}
 .legend .off,table.week .off{{opacity:.4;text-decoration:line-through}}
 table.week th[data-team]{{cursor:pointer}}
 .legend button.unplanned{{font:inherit;font-size:13px;border:1px solid #d3d8de;background:#fff;
   color:inherit;padding:2px 8px;margin:6px 0 0;border-radius:6px;cursor:pointer;display:inline-block}}
 .legend button.unplanned:hover{{background:#eef1f4}}
 .legend .pin{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;
   vertical-align:-1px;background:#b4bac2;border:1px solid #6b7280}}
 .dot{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:-1px}}
 .note{{font-size:12px;color:#6b7280;margin-top:8px}}
 .days{{margin-bottom:10px;display:flex;flex-wrap:wrap;gap:6px}}
 .days button{{font:inherit;font-size:13px;padding:5px 12px;border:1px solid #d3d8de;
   border-radius:999px;background:#fff;color:#1b1f24;cursor:pointer}}
 .days button:hover{{border-color:#9aa3ad}}
 .days button[aria-pressed="true"]{{background:#1F4E78;border-color:#1F4E78;color:#fff}}
 .legend{{margin-bottom:10px;min-height:20px}}
 .tiles{{display:block;font-size:12px;color:#6b7280;margin-bottom:10px}}
 .tiles select{{font-size:12px;margin:0 6px}}
 #tilehint{{color:#9ca3af}}
 table.week{{border-collapse:collapse;margin-bottom:12px;font-size:13px}}
 table.week th,table.week td{{border:1px solid #e2e5ea;padding:6px 12px;text-align:right;
   font-variant-numeric:tabular-nums}}
 table.week th{{background:#f6f7f9;font-weight:600;text-align:center}}
 table.week th:first-child,table.week td:first-child{{text-align:left;font-weight:600}}
 table.week tr.total td,table.week tr.total th{{border-top:2px solid #c3cdd2;
   background:#f6f7f9;font-weight:700}}
 table.week td.zero{{color:#9aa3ad}}
 table.week tr.hist td{{color:#55606b;border-top:2px solid #c3cdd2}}
 table.week tr.hist small{{color:#8a939c}}
 table.week .swatch{{display:inline-block;width:9px;height:9px;border-radius:50%;
   margin-right:6px;vertical-align:-1px}}
</style>
<h1>Bangkok dispatch map</h1>
<div class="sub">{subtitle}</div>

<div class="card">
  <h2 style="margin-top:0">The routes</h2>
  <div id="days" class="days"></div>
  <label class="tiles">Street map background:
  <select id="tiles">
    <option value="">none (offline)</option>
    <option value="esri">Esri street map</option>
    <option value="osm">OpenStreetMap</option>
  </select>
  <span id="tilehint"></span></label>
  <div id="legend" class="legend"></div>
  <div id="map"></div>
  <div class="note">Lines follow the actual streets the teams would drive.
  The grey background is the main road network, drawn from the map data on this
  machine, so the page works with no internet connection. Click a marker for
  the stop's details; a yellow disc is an URGENT job. Click a team in the legend or the table to hide or show
  its route. Grey pins are pending jobs no route takes on any day of the plan; click
  "Not planned" to hide or show them. A multi-day plan shows one day at a time — the whole week at
  once is unreadable, and no crew drives it that way.</div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const ROUTES = {routes};
const DEPOT  = {depot};
const DEPOTS = {depots};
// Each crew's km before this plan, from the Working History sheet.
const HISTORY = {history};
// The one day that was written to the Daily Route sheet, or '' when every
// shown day was. Days after it are drawn but marked provisional: the plan is
// rebuilt every morning, so they will change.
const ISSUED = {issued};
const ROADS  = {roads};
// Pending jobs that no route takes on any day: grey pins, under the routes.
const UNPLANNED = {unplanned};
const map = L.map('map');
map.createPane('unplanned').style.zIndex = 390;     // below the routes (400)
const unplannedLayer = L.layerGroup();
UNPLANNED.forEach(p => L.circleMarker([p.lat, p.lon], {{pane:'unplanned', radius:6,
  color:'#6b7280', weight:1, fillColor:'#b4bac2', fillOpacity:0.9}})
  .bindPopup(p.label).addTo(unplannedLayer));
let unplannedOn = UNPLANNED.length > 0;

// The background is drawn from the road graph on this machine, so the page
// works with no network at all -- and cannot be blocked by anyone's tile
// server. Street tiles are available behind the checkbox for whoever wants
// them and has a connection that is allowed to fetch them.
if (ROADS.length) {{
  L.polyline(ROADS, {{color:'#c7cdd4', weight:1, opacity:0.9,
    interactive:false}}).addTo(map);
}}

// OpenStreetMap refuses a tile request that arrives with no Referer header,
// which is exactly what a page opened as a local file sends. That is why the
// background is blank when this file is double-clicked, on any machine and
// any network. Two ways round it, both offered here:
//
//   - serve this page over http (python tools/serve_map.py), which gives the
//     browser a real address to send, and then OpenStreetMap works;
//   - use Esri's street map, which does not check the Referer, so it works
//     straight from the file.
//
// Neither is the default. The grey background drawn from the local road graph
// needs no network at all.
const TILE_SOURCES = {{
  osm: {{
    url: 'https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    options: {{maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'}},
    hint: 'blank? this one needs the page served over http, not opened as a file'
  }},
  esri: {{
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',
    options: {{maxZoom: 19, attribution: 'Tiles &copy; Esri'}},
    hint: 'needs internet'
  }}
}};

let tiles = null;
const tileHint = document.getElementById('tilehint');
document.getElementById('tiles').addEventListener('change', e => {{
  if (tiles) {{ map.removeLayer(tiles); tiles = null; }}
  const pick = TILE_SOURCES[e.target.value];
  tileHint.textContent = pick ? pick.hint : '';
  if (!pick) return;
  tiles = L.tileLayer(pick.url, pick.options);
  tiles.addTo(map);
  tiles.bringToBack();
}});

// Served over http? Then OpenStreetMap will work, so turn it on without
// making anyone find the dropdown. Opened as a file? Say why it is off.
(function () {{
  const picker = document.getElementById('tiles');
  if (location.protocol === 'http:' || location.protocol === 'https:') {{
    picker.value = 'osm';
    picker.dispatchEvent(new Event('change'));
  }} else {{
    tileHint.textContent =
      'opened as a file - run  python tools/serve_map.py  for street tiles';
  }}
}})();

if (DEPOTS.length) {{
  DEPOTS.forEach(d => L.circleMarker([d[1], d[2]], {{radius:9,color:'#111',fillColor:'#111',fillOpacity:1}})
    .bindPopup('<b>Depot</b><br>' + d[0]).addTo(map));
}} else {{
  L.circleMarker(DEPOT, {{radius:9,color:'#111',fillColor:'#111',fillOpacity:1}})
   .bindPopup('<b>Depot</b><br>all teams start and finish here').addTo(map);
}}

// One Leaflet layer group per crew-day, so a day can be shown on its own.
const layers = ROUTES.map(r => {{
  const group = L.layerGroup();
  const bounds = [DEPOT];
  if (r.line.length) {{
    L.polyline(r.line, {{color:r.colour, weight:4, opacity:0.8}}).addTo(group);
    r.line.forEach(p => bounds.push(p));
  }}
  r.stops.forEach(s => {{
    L.marker([s.lat, s.lon]).bindPopup(s.label).addTo(group);
    // An URGENT job gets a yellow disc, so it can be found at a glance.
    L.circleMarker([s.lat, s.lon], {{radius:13,color:r.colour,weight:s.urgent ? 4 : 2,
      fillColor:s.urgent ? '#ffd84d' : '#fff',fillOpacity:0.95}})
     .bindTooltip(String(s.seq), {{permanent:true,direction:'center',
       className:'seq'}}).addTo(group);
  }});
  return {{route:r, group:group, bounds:bounds}};
}});

const days = [...new Set(ROUTES.map(r => r.date))].filter(d => d !== '').sort();

function unplannedButton() {{
  if (!UNPLANNED.length) return '';
  return '<div><button type="button" class="unplanned' + (unplannedOn ? '' : ' off') +
    '" data-unplanned="1" aria-pressed="' + unplannedOn + '" title="Click to hide or show">' +
    '<i class="pin"></i>Not planned \u2014 ' + UNPLANNED.length + ' jobs</button></div>';
}}

function drawUnplanned() {{
  if (unplannedOn) unplannedLayer.addTo(map); else map.removeLayer(unplannedLayer);
}}

// Teams switched off by clicking them; kept while moving between days.
const hidden = new Set();
let current = null;
function teamOff(t) {{ return hidden.has(t) ? ' off' : ''; }}

function km(v) {{ return v.toFixed(1); }}

// One crew per column, one day per row, the week's total underneath. The flat
// list this replaces ran to twenty entries on a five-day plan and told nobody
// which day anything belonged to.
function weekTable(shown) {{
  const teams = [...new Set(shown.map(l => l.route.team))].sort((a, b) => a - b);
  const colour = {{}};
  shown.forEach(l => {{ colour[l.route.team] = l.route.colour; }});

  const head = '<tr><th></th>' + teams.map(t =>
    '<th data-team="' + t + '" class="' + teamOff(t) + '" title="Click to hide or show">' +
    '<i class="swatch" style="background:' + colour[t] + '"></i>Team ' + t +
    '</th>').join('') + '<th>Day total</th></tr>';

  const perTeam = {{}};
  teams.forEach(t => perTeam[t] = 0);
  let grand = 0;

  const rows = days.map((d, i) => {{
    let dayTotal = 0;
    const cells = teams.map(t => {{
      const l = shown.find(x => x.route.date === d && x.route.team === t);
      const v = l ? l.route.km : 0;
      perTeam[t] += v; dayTotal += v;
      return l ? '<td>' + km(v) + '</td>' : '<td class="zero">&mdash;</td>';
    }}).join('');
    grand += dayTotal;
    return '<tr><td>Day ' + (i + 1) + ' &middot; ' + d + '</td>' + cells +
           '<td>' + km(dayTotal) + '</td></tr>';
  }}).join('');

  const totals = '<tr class="total"><td>Total (km)</td>' +
    teams.map(t => '<td>' + km(perTeam[t]) + '</td>').join('') +
    '<td>' + km(grand) + '</td></tr>';

  // Every crew's distance so far, so the page answers "who has driven most"
  // and not only "who drives most this week".
  let history = '', histNote = '';
  if (HISTORY && HISTORY.before) {{
    const past = t => (HISTORY.km[t - 1] || 0);
    const worked = t => (HISTORY.days[t - 1] || 0);
    const pastAll = teams.reduce((a, t) => a + past(t), 0);
    history =
      '<tr class="hist"><td>Worked before ' + HISTORY.before + '</td>' +
      teams.map(t => '<td>' + km(past(t)) + '<small> &middot; ' + worked(t) +
                     ' d</small></td>').join('') +
      '<td>' + km(pastAll) + '</td></tr>' +
      '<tr class="total"><td>Accumulated incl. this plan (km)</td>' +
      teams.map(t => '<td>' + km(past(t) + perTeam[t]) + '</td>').join('') +
      '<td>' + km(pastAll + grand) + '</td></tr>';
    histNote = ' &middot; "Worked before" = Working History sheet, latest row per ' +
               'crew and day (d = days worked)';
  }}

  const jobs = shown.reduce((a, l) => a + l.route.stops.length, 0);
  const urgent = shown.reduce((a, l) => a + l.route.stops.filter(s => s.urgent).length, 0);
  return '<table class="week">' + head + rows + totals + history + '</table>' +
         '<div class="note" style="margin-top:0">' + jobs +
         ' jobs over ' + days.length + ' working days' +
         (urgent ? ' &middot; ' + urgent + ' URGENT (yellow discs)' : '') + histNote + '</div>';
}}

function dayLegend(shown) {{
  if (!shown.length) return '<span>No routes to draw</span>';
  const legend = shown.map(l =>
    '<button type="button" class="team' + teamOff(l.route.team) + '" data-team="' +
    l.route.team + '" aria-pressed="' + !hidden.has(l.route.team) +
    '" title="Click to hide or show"><i class="dot" style="background:' +
    l.route.colour + '"></i>Team ' + l.route.team + ' \u2014 ' +
    km(l.route.km) + ' km</button>').join('');
  const total = shown.reduce((a, l) => a + l.route.km, 0);
  const jobs  = shown.reduce((a, l) => a + l.route.stops.length, 0);
  const urgent = shown.reduce((a, l) => a + l.route.stops.filter(s => s.urgent).length, 0);
  return legend + '<span style="color:#5b6472">' + jobs + ' jobs, ' +
         km(total) + ' km' + (urgent ? ' &middot; <b style="background:#ffd84d;' +
         'padding:0 5px;border-radius:4px;color:#1b1f24">' + urgent +
         ' URGENT</b>' : '') + '</span>';
}}

function show(day, refit = true) {{
  current = day;
  const inDay = [], drawn = [];
  layers.forEach(l => {{
    const today = (day === null) || (l.route.date === day);
    if (today) inDay.push(l);
    if (today && !hidden.has(l.route.team)) {{ l.group.addTo(map); drawn.push(l); }}
    else map.removeLayer(l.group);
  }});

  // Hidden teams stay in the legend, greyed out, so they can be switched back.
  document.getElementById('legend').innerHTML =
    ((day === null && days.length > 1) ? weekTable(inDay) : dayLegend(inDay)) +
    unplannedButton();
  drawUnplanned();

  document.querySelectorAll('#days button').forEach(b =>
    b.setAttribute('aria-pressed', String(b.dataset.day === String(day))));

  if (!refit) return;                  // hiding a team keeps the view where it is
  const pts = [].concat(...(drawn.length ? drawn : inDay).map(l => l.bounds));
  if (pts.length) map.fitBounds(pts, {{padding:[30,30]}});
  else map.setView(DEPOT, 11);
}}

document.getElementById('legend').addEventListener('click', e => {{
  if (e.target.closest('[data-unplanned]')) {{
    unplannedOn = !unplannedOn;
    show(current, false);
    return;
  }}
  const el = e.target.closest('[data-team]');
  if (!el) return;
  const t = Number(el.dataset.team);
  if (hidden.has(t)) hidden.delete(t); else hidden.add(t);
  show(current, false);
}});

const bar = document.getElementById('days');
if (days.length > 1) {{
  days.forEach(d => {{
    const b = document.createElement('button');
    b.textContent = (ISSUED && d !== ISSUED) ? d + ' (แผนล่วงหน้า)' : d;
    b.dataset.day = d;
    if (ISSUED && d !== ISSUED) b.style.fontStyle = 'italic';
    b.onclick = () => show(d);
    bar.appendChild(b);
  }});
  const all = document.createElement('button');
  all.textContent = 'All ' + days.length + ' days';
  all.dataset.day = 'null';
  all.onclick = () => show(null);
  bar.appendChild(all);
}}

// A plan of several days opens on its first day; one day is shown whole.
show(days.length > 1 ? days[0] : null);
</script>
"""


def crew_history(routes, workbook_path: Path | None = None) -> dict:
    """Each crew's km and days worked before the plan's first day, from the
    Working History sheet. Empty when it cannot be read -- the map is never
    fatal."""
    from datetime import date

    dates = sorted({r["date"] for r in routes if r.get("date")})
    if not dates:
        return {}
    teams = max(int(r["team"]) for r in routes)
    try:
        from dispatch.history import workload_so_far

        km, worked = workload_so_far(
            plan_date=date.fromisoformat(dates[0][:10]), teams=teams,
            metric="km", period="all", path=workbook_path or cfg.WORKBOOK_PATH,
        )
    except Exception as exc:               # noqa: BLE001
        log.info("No crew history for the map (%s)", exc)
        return {}
    return {"before": dates[0][:10], "km": [round(k, 1) for k in km],
            "days": list(worked)}


def _id(value) -> str:
    """Customer No. as text, however Excel or pandas typed it."""
    text = "" if value is None else str(value).strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def unplanned_pins(pending, plan) -> list[dict]:
    """A grey pin for every pending job that no route on any day takes."""
    if pending is None or not len(pending):
        return []
    routed = set()
    if plan is not None and len(plan) and "Customer No." in plan.columns:
        routed = {_id(v) for v in plan["Customer No."]}
    pins = []
    for _, row in pending.iterrows():
        if _id(row.get("Customer No.")) in routed:
            continue
        try:
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (np.isfinite(lat) and np.isfinite(lon)):
            continue
        days = row.get("Days Until Reading", "")
        try:
            days = f" &middot; {int(days)} days to reading"
        except (TypeError, ValueError):
            days = ""
        pins.append({
            "lat": lat, "lon": lon,
            "label": (f"<b>Not planned</b><br>Customer {_id(row.get('Customer No.'))}<br>"
                      f"{row.get('Purpose', '')}<br>"
                      f"Billing Day {row.get('Billing Day', '')} &middot; "
                      f"{row.get('Billing State', '')}{days}"),
        })
    return pins


def render(plan=None, graph_path: Path | None = None,
           out: Path | None = None, note: str = "",
           issued: str = "", pending=None) -> Path:
    """Write the map page and return its path.

    ``plan`` is the plan table (None draws roads only), ``note`` prefixes the
    subtitle, and ``pending`` jobs that no route takes are drawn as grey pins.
    """
    graph_path = graph_path or DEFAULT_CONFIG.routing.graph_path
    out = out or (PROJECT_DIR / "mapdata" / "route_map.html")

    graph = RoadGraph.load(graph_path)
    roads = major_roads(graph)

    routes, subtitle = [], (
        (f"{note} &middot; " if note else "")
        + f"Road graph: {graph.meta['nodes']:,} junctions, "
        f"{graph.meta['edges']:,} one-way links, built {graph.meta['built']}"
    )

    depot = (DEFAULT_CONFIG.fleet.depot_lat, DEFAULT_CONFIG.fleet.depot_lon)
    try:                                   # branch depots from workforce.xlsx
        from dispatch.workforce import load_depots
        depots = {n: (d.lat, d.lon) for n, d in load_depots(announce=False).items()}
    except Exception as exc:               # noqa: BLE001 - the map is never fatal
        log.info("No branch depots for the map (%s); using the config depot", exc)
        depots = {}
    history = {}
    if plan is not None and len(plan):
        routes = route_lines(graph, plan, depot, depots=depots)
        history = crew_history(routes)
        total = sum(r["km"] for r in routes)
        dates = sorted({r["date"] for r in routes if r["date"]})
        subtitle += f" &middot; {len(plan)} jobs, {total:.1f} km"
        if len(dates) > 1:
            subtitle += (
                f" over {len(dates)} working days ({dates[0]} to {dates[-1]})"
            )

    # Show when the map was made, on the page itself.
    subtitle += (
        " &middot; drawn "
        + datetime.now().strftime("%d %b %Y %H:%M")
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        PAGE.format(
            subtitle=subtitle,
            routes=json.dumps(routes),
            roads=json.dumps(roads, separators=(",", ":")),
            depot=json.dumps([DEFAULT_CONFIG.fleet.depot_lat,
                              DEFAULT_CONFIG.fleet.depot_lon]),
            depots=json.dumps([[n, lat, lon] for n, (lat, lon) in depots.items()]),
            history=json.dumps(history),
            issued=json.dumps(issued or ""),
            unplanned=json.dumps(unplanned_pins(pending, plan)),
        ),
        encoding="utf-8",
    )
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    args = parse_args(argv)

    plan = read_plan_from_sheet(args.workbook)
    try:                                   # grey pins: the jobs not in the plan
        from dispatch.workforce import load_pending_jobs
        pending = load_pending_jobs(args.workbook)
        pending = pending[pending["has_coords"]]
    except Exception as exc:               # noqa: BLE001 - the map is never fatal
        log.info("No pending jobs for the map (%s)", exc)
        pending = None

    out = render(plan, args.graph, args.out, pending=pending)
    print(f"\nMap written to {out}\nDouble-click it to open in a browser.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
