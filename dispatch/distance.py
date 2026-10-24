"""The distance-provider interface and the checks every provider must pass.

There is no straight-line fallback: if the road graph can't answer, the run fails.
``haversine_matrix`` is only used to catch impossible road distances.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import numpy as np

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088


def haversine_matrix(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Pairwise great-circle distances (km) between N points, vectorized.

    Used to sanity-check road distances, never to plan routes.
    """
    lat = np.radians(np.asarray(lats, dtype=float))[:, None]
    lon = np.radians(np.asarray(lons, dtype=float))[:, None]
    dlat = lat - lat.T
    dlon = lon - lon.T
    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat) * np.cos(lat.T) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance (km) between matching pairs of points."""
    p1, p2 = np.radians(np.asarray(lat1, dtype=float)), np.radians(np.asarray(lat2, dtype=float))
    dlon = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    a = np.sin((p2 - p1) / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


#: Allowance for rounding in the check below, in km (2 m).
_VERIFY_TOLERANCE_KM = 0.002


def calibrate_driving_times(
    km: np.ndarray, minutes: np.ndarray, target_kmh: float | None
) -> np.ndarray:
    """Scale all driving times by one factor so the average speed is ``target_kmh``.

    Returns the times unchanged when ``target_kmh`` is None.
    """
    if target_kmh is None:
        return minutes

    if target_kmh <= 0:
        raise ValueError(f"target_average_speed_kmh must be positive, got {target_kmh}")

    off = ~np.eye(len(km), dtype=bool)
    total_km = float(km[off].sum())
    total_hours = float(minutes[off].sum()) / 60.0
    if total_km <= 0 or total_hours <= 0:
        return minutes

    free_flow_kmh = total_km / total_hours
    factor = free_flow_kmh / target_kmh
    log.debug(
        "Driving times calibrated: map free-flow %.1f km/h -> %.1f km/h "
        "(x%.2f)", free_flow_kmh, target_kmh, factor,
    )
    return minutes * factor


def verify_road_distances(
    km: np.ndarray,
    lats: np.ndarray,
    lons: np.ndarray,
    source: str = "the engine",
    snap_km: np.ndarray | None = None,
) -> None:
    """Raise if a road distance is shorter than the straight line allows.

    ``snap_km`` is how far each point was moved to reach the road, which the route
    may legitimately save. A 2 m allowance absorbs rounding.
    """
    straight = haversine_matrix(lats, lons)

    if snap_km is None:
        slack = np.zeros_like(straight)
    else:
        s = np.asarray(snap_km, dtype=float)
        slack = s[:, None] + s[None, :]

    floor = np.maximum(straight - slack - _VERIFY_TOLERANCE_KM, 0.0) * 0.99
    impossible = km < floor
    if impossible.any():
        i, j = np.argwhere(impossible)[0]
        detail = (
            f" (allowing {slack[i, j]:.2f} km of road-snapping slack)"
            if snap_km is not None else ""
        )
        raise ValueError(
            f"{source} reported {km[i, j]:.2f} km between points {i} and {j}, "
            f"which is shorter than the {straight[i, j]:.2f} km straight line"
            f"{detail} -- impossible. Most likely the coordinates were used as "
            f"lat,lon instead of lon,lat, or the map does not cover this area."
        )


class DistanceProvider(ABC):
    """Supplies the travel matrices the solver and scheduler need."""

    @abstractmethod
    def matrix_km(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        """Return an NxN matrix of road distances in km."""

    @abstractmethod
    def matrix_minutes(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        """Return an NxN matrix of driving times in minutes."""


def make_provider(routing) -> DistanceProvider:
    """Build the routing engine named by ``RoutingConfig.backend``.

    The local road graph is the only backend. There is no straight-line
    option -- see the ``RoutingConfig`` docstring for why.
    """
    backend = str(routing.backend).strip().lower()

    if backend == "local_graph":
        from .road_graph import LocalGraphProvider

        log.debug(
            "Routing engine: local road graph (%s), %s-optimal paths",
            routing.graph_path, getattr(routing, "path_metric", "time"),
        )
        return LocalGraphProvider(
            path_metric=getattr(routing, "path_metric", "time"),
            graph_path=routing.graph_path,
            snap_max_km=routing.snap_max_km,
            verify_against_straight_line=routing.verify_against_straight_line,
            target_average_speed_kmh=routing.target_average_speed_kmh,
            matrix_cache_path=(
                routing.matrix_cache_path
                if getattr(routing, "use_matrix_cache", True) else None
            ),
        )

    raise ValueError(
        f"Unknown routing backend {routing.backend!r}. Use 'local_graph'. "
        f"Straight-line estimation is not an option by design."
    )
