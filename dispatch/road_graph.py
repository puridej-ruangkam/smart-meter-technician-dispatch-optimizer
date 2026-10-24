"""Shortest paths on the pre-built OpenStreetMap road graph, using scipy's Dijkstra.

Routes minimise time or distance, and the other quantity is measured along the
same route. Turn restrictions and live traffic are not modelled.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .distance import (
    DistanceProvider,
    calibrate_driving_times,
    haversine_km,
    verify_road_distances,
)

log = logging.getLogger(__name__)

GRAPH_FORMAT_VERSION = 1


class RoadGraphError(RuntimeError):
    """The road graph is missing, unreadable, or cannot serve this request."""


class RoadGraph:
    """Node coordinates plus a directed sparse road network.

    Loaded from the ``.npz`` produced by ``tools/build_bangkok_graph.py``.
    """

    memory_budget_mb: float = 400.0       # peak for the shortest-path search

    def __init__(self, lat, lon, indptr, indices, seconds, meters, meta):
        self.lat = lat
        self.lon = lon
        self.meta = meta
        self._seconds_csr = self._csr(indptr, indices, seconds)
        self._meters_flat = meters          # parallel to `indices`
        self._seconds_flat = seconds        # parallel to `indices`
        self._indptr = indptr
        self._indices = indices
        self._tree = None                   # built lazily; scipy import is slow

    @staticmethod
    def _csr(indptr, indices, data):
        from scipy.sparse import csr_matrix

        n = len(indptr) - 1
        m = csr_matrix((data, indices, indptr), shape=(n, n))
        m.sort_indices()                    # required for the edge lookup below
        return m

    @classmethod
    def load(cls, path: Path | str) -> "RoadGraph":
        path = Path(path)                   # tolerate a plain string
        if not path.exists():
            raise RoadGraphError(
                f"Road graph not found at {path}. Build it once with:\n"
                f"    python tools\\build_bangkok_graph.py"
            )
        try:
            z = np.load(path, allow_pickle=False)
            version = int(z["format_version"][0])
            if version != GRAPH_FORMAT_VERSION:
                raise RoadGraphError(
                    f"Road graph at {path} is format v{version}, this code "
                    f"expects v{GRAPH_FORMAT_VERSION}. Rebuild it with "
                    f"python tools\\build_bangkok_graph.py"
                )
            meta = {
                "nodes": int(z["node_lat"].size),
                "edges": int(z["indices"].size),
                "bbox": z["bbox"].tolist(),
                "built": str(z["built_utc"][0]),
            }
            graph = cls(
                z["node_lat"], z["node_lon"], z["indptr"], z["indices"],
                z["edge_seconds"], z["edge_meters"], meta,
            )
        except RoadGraphError:
            raise
        except Exception as exc:
            raise RoadGraphError(f"Could not read the road graph {path}: {exc}") from exc

        log.debug(
            "Road graph loaded: %s nodes, %s directed edges, built %s",
            f"{meta['nodes']:,}", f"{meta['edges']:,}", meta["built"],
        )
        return graph

    # -- spatial lookup ----------------------------------------------------

    def _projected(self, lat, lon):
        """Local equirectangular projection in km -- adequate over one city."""
        lat0 = float(np.mean(self.lat))
        return np.column_stack([
            np.asarray(lon, dtype=float) * 111.320 * np.cos(np.radians(lat0)),
            np.asarray(lat, dtype=float) * 110.574,
        ])

    @property
    def routable_nodes(self) -> np.ndarray:
        """Indices of nodes in the main connected road network, the only ones meters snap to."""
        if getattr(self, "_routable_cache", None) is None:
            from scipy.sparse.csgraph import connected_components

            count, labels = connected_components(
                self._seconds_csr, directed=True, connection="strong"
            )
            main = np.bincount(labels).argmax()
            self._routable_cache = np.flatnonzero(labels == main)
            if count > 1:
                log.debug(
                    "Road network: %s of %s nodes are mutually reachable; "
                    "ignoring %s disconnected fragments when snapping",
                    f"{len(self._routable_cache):,}", f"{len(self.lat):,}",
                    f"{count - 1:,}",
                )
        return self._routable_cache

    def snap(self, lats, lons, max_km: float) -> tuple[np.ndarray, np.ndarray]:
        """Return (node indices, snap distance km) for each input point.

        Only nodes on the main road network are candidates -- see
        :attr:`routable_nodes`.
        """
        from scipy.spatial import cKDTree

        if self._tree is None:
            usable = self.routable_nodes
            self._tree_nodes = usable
            self._tree = cKDTree(
                self._projected(self.lat[usable], self.lon[usable])
            )
        _, local = self._tree.query(self._projected(lats, lons), k=1)
        idx = self._tree_nodes[local]
        # Snap distance on the sphere, like the straight line it is compared with.
        dist_km = haversine_km(lats, lons, self.lat[idx], self.lon[idx])

        too_far = dist_km > max_km
        if too_far.any():
            worst = int(np.argmax(dist_km))
            raise RoadGraphError(
                f"{int(too_far.sum())} location(s) are more than {max_km} km "
                f"from any road in the map. Worst: point {worst} at "
                f"({lats[worst]:.6f}, {lons[worst]:.6f}), {dist_km[worst]:.2f} km "
                f"away. Either its coordinates are wrong, or it lies outside "
                f"the mapped area -- widen the bounding box and rebuild."
            )
        return idx.astype(np.int64), dist_km

    # -- routing -----------------------------------------------------------

    @property
    def _edge_keys(self) -> np.ndarray:
        """Sorted ``source * N + target`` for every edge, so edges can be looked up in bulk."""
        if getattr(self, "_edge_keys_cache", None) is None:
            n = len(self.lat)
            sources = np.repeat(
                np.arange(len(self._indptr) - 1), np.diff(self._indptr)
            )
            self._edge_keys_cache = sources.astype(np.int64) * n + self._indices
        return self._edge_keys_cache

    def _edge_values_bulk(
        self, u: np.ndarray, v: np.ndarray, values: np.ndarray
    ) -> np.ndarray:
        """Price many directed edges u->v at once from a per-edge array."""
        n = len(self.lat)
        pos = np.searchsorted(self._edge_keys, u.astype(np.int64) * n + v)
        pos = np.clip(pos, 0, len(values) - 1)
        return values[pos]

    def path_nodes(
        self, source: int, target: int, weight: str = "time"
    ) -> list[int]:
        """The node sequence of the route from source to target, for drawing on the map.

        ``weight`` must match the one the plan used.
        """
        from scipy.sparse.csgraph import dijkstra

        _, preds = dijkstra(
            self._csr_for(self._check_weight(weight)), directed=True,
            indices=[source], return_predecessors=True,
        )
        pred = preds[0]
        path, node = [], int(target)
        while node != source and node >= 0:
            path.append(node)
            node = int(pred[node])
            if len(path) > 1_000_000:
                raise RoadGraphError("Path reconstruction did not terminate")
        if node != source:
            return []                       # unreachable
        path.append(source)
        return path[::-1]

    # -- which weight the search minimises ----------------------------------
    # Seconds give the quickest route, metres the shortest.

    WEIGHTS = ("time", "distance")

    @staticmethod
    def _check_weight(weight: str) -> str:
        w = str(weight).strip().lower()
        if w not in RoadGraph.WEIGHTS:
            raise ValueError(
                f"weight must be one of {RoadGraph.WEIGHTS}, got {weight!r}"
            )
        return w

    @property
    def _meters_csr(self):
        """The network weighted by length instead of driving time."""
        if getattr(self, "_meters_csr_cache", None) is None:
            self._meters_csr_cache = self._csr(
                self._indptr, self._indices, self._meters_flat
            )
        return self._meters_csr_cache

    def _edge_values(self, weight: str) -> np.ndarray:
        """The per-edge array parallel to ``indices`` for this weight."""
        return (
            self._meters_flat if weight == "distance"
            else self._seconds_flat
        )

    def _csr_for(self, weight: str, reverse: bool = False):
        """The network to search for this weight, forwards or reversed."""
        forward = self._meters_csr if weight == "distance" else self._seconds_csr
        if not reverse:
            return forward
        attr = f"_reverse_csr_cache_{weight}"
        if getattr(self, attr, None) is None:
            rev = forward.T.tocsr()
            rev.sort_indices()
            setattr(self, attr, rev)
        return getattr(self, attr)

    def matrices(
        self, node_idx: np.ndarray, weight: str = "time"
    ) -> tuple[np.ndarray, np.ndarray]:
        """(km, minutes) between every pair of the given nodes, both measured along the same route."""
        km, minutes = self.matrices_between(node_idx, node_idx, weight=weight)
        np.fill_diagonal(km, 0.0)
        np.fill_diagonal(minutes, 0.0)
        return km, minutes

    def matrices_between(
        self,
        src_nodes: np.ndarray,
        dst_nodes: np.ndarray,
        reverse: bool = False,
        weight: str = "time",
    ) -> tuple[np.ndarray, np.ndarray]:
        """(km, minutes) from each source node to each destination node.

        With ``reverse=True`` entry ``[i][j]`` is the trip from ``dst_nodes[j]`` to
        ``src_nodes[i]``. Costs one search per source.
        """
        from scipy.sparse.csgraph import dijkstra

        weight = self._check_weight(weight)
        src = np.asarray(src_nodes, dtype=np.int64)
        dst = np.asarray(dst_nodes, dtype=np.int64)
        n_src, n_dst = len(src), len(dst)
        km = np.zeros((n_src, n_dst), dtype=float)
        minutes = np.zeros((n_src, n_dst), dtype=float)
        if n_src == 0 or n_dst == 0:
            return km, minutes

        csr = self._csr_for(weight, reverse=reverse)
        # The search minimises one weight; the other is then measured along
        # the path it chose, never solved for separately.
        measured = self._edge_values(
            "time" if weight == "distance" else "distance"
        )

        # Batch the sources so scipy's full-length rows stay within `memory_budget_mb`.
        per_source_mb = len(self.lat) * 12 / 1e6
        batch = max(1, int(self.memory_budget_mb / max(per_source_mb, 1e-9)))
        if batch < n_src:
            log.debug(
                "Computing %d x %d routes in batches of %d to stay within "
                "~%d MB of memory", n_src, n_dst, batch, self.memory_budget_mb,
            )

        for start in range(0, n_src, batch):
            stop = min(start + batch, n_src)
            cost, preds = dijkstra(
                csr, directed=True, indices=src[start:stop],
                return_predecessors=True,
            )
            if weight == "distance":
                km[start:stop] = cost[:, dst] / 1000.0
                self._fill_measured(
                    minutes, cost, preds, src, dst, start, stop, reverse,
                    measured, scale=1.0 / 60.0,
                )
            else:
                minutes[start:stop] = cost[:, dst] / 60.0
                self._fill_measured(
                    km, cost, preds, src, dst, start, stop, reverse,
                    measured, scale=1.0 / 1000.0,
                )

        return km, minutes

    def _fill_measured(
        self, out, cost, preds, src, dst, start, stop, reverse=False,
        values=None, scale=1.0,
    ) -> None:
        """Add up ``values`` (metres or seconds) along each path found in this batch."""
        if values is None:
            values = self._meters_flat
        n = len(dst)
        for offset, i in enumerate(range(start, stop)):
            pred_i = preds[offset]
            source = int(src[i])

            # Walk every target back to the source together, one hop per step.
            reachable = np.isfinite(cost[offset, dst])
            current = dst.astype(np.int64).copy()
            total_m = np.zeros(n, dtype=float)
            walking = reachable & (current != source)

            guard = 0
            while walking.any():
                here = current[walking]
                prev = pred_i[here]
                if (prev < 0).any():        # a broken predecessor chain
                    idx = np.flatnonzero(walking)[prev < 0]
                    reachable[idx] = False
                    walking[idx] = False
                    here, prev = current[walking], pred_i[current[walking]]
                    if here.size == 0:
                        break
                # On the reversed network a step prev->here in the predecessor
                # chain is the original road here->prev, so the lookup flips.
                total_m[walking] += (
                    self._edge_values_bulk(here, prev, values) if reverse
                    else self._edge_values_bulk(prev, here, values)
                )
                current[walking] = prev
                walking &= current != source
                guard += 1
                if guard > 5_000_000:       # cannot happen on a valid graph
                    raise RoadGraphError("Path reconstruction did not terminate")

            out[i] = np.where(reachable, total_m * scale, np.inf)


class LocalGraphProvider(DistanceProvider):
    """DistanceProvider backed by the locally built road graph."""

    def __init__(
        self,
        graph_path: Path,
        snap_max_km: float = 2.0,
        verify_against_straight_line: bool = True,
        target_average_speed_kmh: float | None = None,
        matrix_cache_path: Path | None = None,
        path_metric: str = "time",
    ):
        self.graph_path = Path(graph_path)
        self.snap_max_km = snap_max_km
        self.verify_against_straight_line = verify_against_straight_line
        self.target_average_speed_kmh = target_average_speed_kmh
        # "time" finds the quickest road, "distance" the shortest. See
        # RoadGraph.WEIGHTS: the two disagree about 77% of the time.
        self.path_metric = RoadGraph._check_weight(path_metric)
        # None switches the on-disk cache off; every run then measures the
        # whole table from the graph, as this class originally did.
        self.matrix_cache_path = (
            None if matrix_cache_path is None else Path(matrix_cache_path)
        )
        self._disk_cache = None
        self._graph: RoadGraph | None = None
        self._cache_key: tuple | None = None
        self._cache: tuple[np.ndarray, np.ndarray] | None = None

    @property
    def graph(self) -> RoadGraph:
        if self._graph is None:
            self._graph = RoadGraph.load(self.graph_path)
        return self._graph

    def matrix_km(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        return self._matrices(lats, lons)[0]

    def matrix_minutes(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        return self._matrices(lats, lons)[1]

    def _raw_matrices(self, node_idx) -> tuple[np.ndarray, np.ndarray]:
        """Uncalibrated (km, minutes), through the on-disk cache if enabled."""
        if self.matrix_cache_path is None:
            return self.graph.matrices(node_idx, weight=self.path_metric)

        from .matrix_cache import TravelMatrixCache

        if self._disk_cache is None:
            self._disk_cache = TravelMatrixCache(
                self._cache_path_for_metric(),
                graph_built=str(self.graph.meta["built"]),
                weight=self.path_metric,
            )
        km, minutes = self._disk_cache.matrices(self.graph, node_idx)
        self._disk_cache.save()
        return km, minutes

    def _cache_path_for_metric(self) -> Path:
        """One cache file per metric, so the two kinds of table are never mixed."""
        path = Path(self.matrix_cache_path)
        if self.path_metric == "time":
            return path                      # the file that already exists
        return path.with_name(f"{path.stem}.{self.path_metric}{path.suffix}")

    def _matrices(self, lats, lons) -> tuple[np.ndarray, np.ndarray]:
        key = (
            tuple(np.round(np.asarray(lats, dtype=float), 7)),
            tuple(np.round(np.asarray(lons, dtype=float), 7)),
        )
        if self._cache_key == key and self._cache is not None:
            return self._cache

        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)

        from .progress import report
        report(15, "คำนวณระยะทางบนถนนจริง", until=28, seconds=40)

        node_idx, snap_km = self.graph.snap(lats, lons, self.snap_max_km)
        if snap_km.max() > 0.25:
            log.debug(
                "Snapped %d points to the road network, farthest %.0f m",
                len(node_idx), snap_km.max() * 1000,
            )

        km, minutes = self._raw_matrices(node_idx)

        off_diagonal = ~np.eye(len(node_idx), dtype=bool)
        unreachable = ~np.isfinite(km) & off_diagonal
        if unreachable.any():
            # Should not happen, since snapping uses the main network only.
            stranded = sorted({int(i) for i, _ in np.argwhere(unreachable)})
            raise RoadGraphError(
                f"{int(unreachable.sum())} pairs of locations have no route "
                f"between them, involving point(s) {stranded[:10]}"
                f"{' and others' if len(stranded) > 10 else ''}. Point 0 is "
                f"the depot; the rest are meters in the order given. Check "
                f"those coordinates, or widen the map's bounding box and "
                f"rebuild. Refusing to plan rather than pricing an impossible "
                f"leg at some arbitrary huge number."
            )

        if self.verify_against_straight_line:
            verify_road_distances(
                km, lats, lons, source="the local road graph", snap_km=snap_km
            )

        minutes = calibrate_driving_times(km, minutes, self.target_average_speed_kmh)

        log.debug(
            "Road graph table (%s-optimal paths): %d points, mean leg "
            "%.2f km / %.1f min",
            self.path_metric, len(node_idx),
            float(km[km > 0].mean()) if (km > 0).any() else 0.0,
            float(minutes[minutes > 0].mean()) if (minutes > 0).any() else 0.0,
        )
        self._cache_key, self._cache = key, (km, minutes)
        return self._cache
