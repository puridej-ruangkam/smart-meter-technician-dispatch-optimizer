"""Build the Bangkok road graph from an OpenStreetMap extract.

    python tools/build_bangkok_graph.py --force

Needs pyrosm and 3 to 4 GB of free memory.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from dispatch.road_graph import GRAPH_FORMAT_VERSION  # noqa: E402

log = logging.getLogger("build_graph")

# Bangkok + นนทบุรี, ปทุมธานี, สมุทรปราการ, สมุทรสาคร, นครปฐม, with margin so
# routes near the edge can still find through-roads.
BBOX = [99.80, 13.30, 101.10, 14.30]          # left, bottom, right, top

SOURCE_URL = "https://download.geofabrik.de/asia/thailand-latest.osm.pbf"

# Speeds in km/h by road class, used where the map has no maxspeed tag.
DEFAULT_SPEED_KMH = {
    "motorway": 80, "motorway_link": 45,
    "trunk": 60, "trunk_link": 40,
    "primary": 45, "primary_link": 35,
    "secondary": 40, "secondary_link": 30,
    "tertiary": 35, "tertiary_link": 25,
    "unclassified": 30, "residential": 25,
    "living_street": 15, "service": 15,
    "road": 30,
}
FALLBACK_SPEED_KMH = 30.0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build the Bangkok road graph")
    p.add_argument(
        "--pbf", type=Path, default=PROJECT_DIR / "mapdata" / "thailand-latest.osm.pbf",
        help="OpenStreetMap extract to read",
    )
    p.add_argument(
        "--out", type=Path, default=PROJECT_DIR / "mapdata" / "bangkok_road_graph.npz",
        help="Where to save the graph",
    )
    p.add_argument("--force", action="store_true", help="Rebuild even if the graph exists")
    p.add_argument(
        "--keep-source", action="store_true",
        help="Keep the .osm.pbf after building (default: delete, it is large)",
    )
    return p.parse_args(argv)


def check_source(pbf: Path) -> None:
    if pbf.exists():
        log.info("Source map: %s (%.0f MB)", pbf, pbf.stat().st_size / 1e6)
        return
    raise SystemExit(
        f"\nMap data not found: {pbf}\n\n"
        f"Download it once (about 310 MB), then re-run this script:\n\n"
        f"    mkdir \"{pbf.parent}\"\n"
        f"    curl.exe -L -o \"{pbf}\" {SOURCE_URL}\n\n"
        f"Geofabrik publishes no Bangkok-only extract, so the national file is\n"
        f"downloaded and this script keeps only the metropolitan area.\n"
    )


def parse_speeds(edges) -> np.ndarray:
    """km/h per edge: the map's maxspeed if usable, else a road-class default."""
    import pandas as pd

    highway = edges["highway"].astype(str) if "highway" in edges else None
    defaults = (
        highway.map(DEFAULT_SPEED_KMH).fillna(FALLBACK_SPEED_KMH)
        if highway is not None
        else pd.Series(FALLBACK_SPEED_KMH, index=edges.index)
    )

    if "maxspeed" not in edges:
        return defaults.to_numpy(dtype=float)

    # maxspeed is free text: "50", "50 km/h", "30 mph", lists, "none", ...
    raw = edges["maxspeed"].astype(str).str.lower().str.strip()
    mph = raw.str.contains("mph", na=False)
    numeric = pd.to_numeric(raw.str.extract(r"(\d+)", expand=False), errors="coerce")
    numeric = numeric.where(~mph, numeric * 1.609)

    speeds = numeric.where(numeric.between(5, 130), defaults)
    n_tagged = int(numeric.between(5, 130).sum())
    log.info(
        "Speeds: %s edges from the map's maxspeed tag, %s from road-class defaults",
        f"{n_tagged:,}", f"{len(edges) - n_tagged:,}",
    )
    return speeds.to_numpy(dtype=float)


def build(pbf: Path, out: Path, keep_source: bool) -> None:
    try:
        from pyrosm import OSM
    except ImportError:
        raise SystemExit(
            "pyrosm is not installed. Run:  pip install --user pyrosm scipy"
        )

    t0 = time.time()
    log.info("Reading the drivable network inside %s ...", BBOX)
    log.info("This is the slow step -- several minutes and a few GB of memory.")
    osm = OSM(str(pbf), bounding_box=BBOX)
    nodes, edges = osm.get_network(network_type="driving", nodes=True)
    log.info(
        "Parsed %s nodes and %s ways in %.0f s",
        f"{len(nodes):,}", f"{len(edges):,}", time.time() - t0,
    )

    # --- node table -------------------------------------------------------
    node_ids = nodes["id"].to_numpy()
    order = np.argsort(node_ids)
    node_ids = node_ids[order]
    node_lat = nodes["lat"].to_numpy(dtype=float)[order]
    node_lon = nodes["lon"].to_numpy(dtype=float)[order]

    # --- edge table -------------------------------------------------------
    u = np.searchsorted(node_ids, edges["u"].to_numpy())
    v = np.searchsorted(node_ids, edges["v"].to_numpy())

    if "length" in edges:
        length_m = edges["length"].to_numpy(dtype=float)
    else:  # derive from node coordinates if the extract lacks lengths
        log.info("No 'length' column; deriving edge lengths from coordinates")
        dlat = np.radians(node_lat[v] - node_lat[u])
        dlon = np.radians(node_lon[v] - node_lon[u])
        mlat = np.radians((node_lat[v] + node_lat[u]) / 2)
        a = np.sin(dlat / 2) ** 2 + np.cos(mlat) ** 2 * np.sin(dlon / 2) ** 2
        length_m = 2 * 6371008.8 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    speed_kmh = parse_speeds(edges)
    seconds = length_m / (speed_kmh * 1000.0 / 3600.0)

    valid = (
        (u != v) & np.isfinite(length_m) & (length_m > 0) & np.isfinite(seconds)
    )
    u, v, length_m, seconds = u[valid], v[valid], length_m[valid], seconds[valid]
    log.info("Kept %s usable ways", f"{valid.sum():,}")

    # --- direction --------------------------------------------------------
    if "oneway" in edges:
        oneway_raw = edges["oneway"].astype(str).str.lower().to_numpy()[valid]
        forward_only = np.isin(oneway_raw, ["yes", "true", "1"])
        reverse_only = oneway_raw == "-1"
    else:
        log.warning("No 'oneway' column -- treating every road as two-way")
        forward_only = np.zeros(len(u), dtype=bool)
        reverse_only = np.zeros(len(u), dtype=bool)

    log.info(
        "One-way roads: %s forward, %s reversed, %s two-way",
        f"{forward_only.sum():,}", f"{reverse_only.sum():,}",
        f"{(~forward_only & ~reverse_only).sum():,}",
    )

    fwd = ~reverse_only               # u->v exists unless the way is reversed
    rev = ~forward_only               # v->u exists unless the way is one-way
    src = np.concatenate([u[fwd], v[rev]])
    dst = np.concatenate([v[fwd], u[rev]])
    e_m = np.concatenate([length_m[fwd], length_m[rev]])
    e_s = np.concatenate([seconds[fwd], seconds[rev]])

    # --- directed CSR -----------------------------------------------------
    from scipy.sparse import coo_matrix

    n = len(node_ids)
    csr_s = coo_matrix((e_s, (src, dst)), shape=(n, n)).tocsr()
    csr_s.sort_indices()
    # Same sparsity pattern, so metres line up with the seconds CSR indices.
    csr_m = coo_matrix((e_m, (src, dst)), shape=(n, n)).tocsr()
    csr_m.sort_indices()
    if csr_m.nnz != csr_s.nnz:
        raise SystemExit("Internal error: distance and time graphs disagree")

    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        format_version=np.array([GRAPH_FORMAT_VERSION]),
        node_lat=node_lat, node_lon=node_lon,
        indptr=csr_s.indptr, indices=csr_s.indices,
        edge_seconds=csr_s.data, edge_meters=csr_m.data,
        bbox=np.array(BBOX),
        built_utc=np.array([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")]),
    )
    log.info(
        "Saved %s -- %s nodes, %s directed edges, %.0f MB, total %.0f s",
        out, f"{n:,}", f"{csr_s.nnz:,}", out.stat().st_size / 1e6, time.time() - t0,
    )

    if not keep_source:
        pbf.unlink(missing_ok=True)
        log.info("Deleted the source extract (pass --keep-source to retain it)")


def verify(out: Path) -> None:
    """Route between two fixed points in Bangkok and check the answer is sane."""
    from dispatch.distance import haversine_matrix
    from dispatch.road_graph import LocalGraphProvider

    lats = np.array([13.7563, 13.7462])         # central Bangkok -> Siam
    lons = np.array([100.5018, 100.5347])
    straight = haversine_matrix(lats, lons)[0, 1]

    provider = LocalGraphProvider(out)
    km = provider.matrix_km(lats, lons)[0, 1]
    minutes = provider.matrix_minutes(lats, lons)[0, 1]

    log.info("Verification: central Bangkok -> Siam = %.2f km, %.1f min by road", km, minutes)
    log.info("              straight-line distance = %.2f km", straight)

    if km < straight:
        raise SystemExit(
            f"FAILED: {km:.2f} km by road is shorter than the {straight:.2f} km "
            f"straight line, which is impossible. Do not use this graph."
        )
    log.info("Verification passed (road distance is %.0f%% of straight line).",
             km / straight * 100)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    args = parse_args(argv)

    if args.out.exists() and not args.force:
        log.info("%s already exists. Use --force to rebuild.", args.out)
    else:
        check_source(args.pbf)
        build(args.pbf, args.out, args.keep_source)

    verify(args.out)
    print("\nDone. Next step:  python main.py --dry-run\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
