"""Travel matrix kept on disk between runs, keyed by road-graph node.

Each new meter costs one forward and one reverse search. Values are stored
uncalibrated, and the cache is discarded when the road graph changes.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

CACHE_FORMAT_VERSION = 1

# Above this the file is large enough to be worth mentioning; 5,000 meters is
# about 400 MB, at which point someone should think about pruning it.
_WARN_NODES = 3_000


class TravelMatrixCache:
    """Persistent road-distance and driving-time table, keyed by graph node."""

    def __init__(self, path: Path | str, graph_built: str = "",
                 weight: str = "time"):
        self.path = Path(path)
        self.graph_built = graph_built
        # "time" or "distance": the two kinds of table must never be mixed.
        self.weight = str(weight).strip().lower()
        self.nodes: np.ndarray = np.empty(0, dtype=np.int64)
        self.km: np.ndarray = np.empty((0, 0), dtype=np.float32)
        self.minutes: np.ndarray = np.empty((0, 0), dtype=np.float32)
        self._position: dict[int, int] = {}
        self._dirty = False
        self._loaded = False

    # -- disk --------------------------------------------------------------

    def load(self) -> None:
        """Read the cache, or start an empty one if it is missing or stale."""
        if self._loaded:
            return
        self._loaded = True
        if not self.path.exists():
            log.debug("No travel matrix cache yet; it will be built at %s", self.path)
            return
        try:
            z = np.load(self.path, allow_pickle=False)
            if int(z["format_version"][0]) != CACHE_FORMAT_VERSION:
                raise ValueError("cache written by a different version")
            built = str(z["graph_built"][0])
            if self.graph_built and built != self.graph_built:
                log.warning(
                    "Travel matrix cache was built against a road graph from "
                    "%s but the current graph is from %s. Discarding it -- "
                    "distances from the old network would be wrong.",
                    built, self.graph_built,
                )
                return
            # Older files have no metric key and hold quickest routes.
            stored = str(z["weight"][0]) if "weight" in z.files else "time"
            if stored != self.weight:
                log.warning(
                    "Travel matrix cache at %s holds %s-optimal routes but "
                    "this run wants %s-optimal ones. Discarding it rather "
                    "than mixing two different sets of roads.",
                    self.path, stored, self.weight,
                )
                return
            self.nodes = z["nodes"].astype(np.int64)
            self.km = z["km"].astype(np.float32)
            self.minutes = z["minutes"].astype(np.float32)
        except Exception as exc:                      # a corrupt cache is not fatal
            log.warning(
                "Could not read the travel matrix cache at %s (%s). "
                "Rebuilding it from scratch.", self.path, exc,
            )
            self.nodes = np.empty(0, dtype=np.int64)
            self.km = np.empty((0, 0), dtype=np.float32)
            self.minutes = np.empty((0, 0), dtype=np.float32)
            return

        self._position = {int(n): i for i, n in enumerate(self.nodes)}
        log.debug(
            "Travel matrix cache: %d locations already known (%.1f MB)",
            len(self.nodes), (self.km.nbytes + self.minutes.nbytes) / 1e6,
        )

    def save(self) -> None:
        """Write the cache back, but only if something was added."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.npz")
        np.savez_compressed(
            tmp,
            format_version=np.array([CACHE_FORMAT_VERSION]),
            graph_built=np.array([self.graph_built]),
            weight=np.array([self.weight]),
            nodes=self.nodes,
            km=self.km,
            minutes=self.minutes,
        )
        tmp.replace(self.path)                # atomic: never a half-written cache
        self._dirty = False
        log.debug(
            "Travel matrix cache saved: %d locations, %.1f MB at %s",
            len(self.nodes), self.path.stat().st_size / 1e6, self.path,
        )

    # -- lookup ------------------------------------------------------------

    def matrices(
        self, graph, node_idx: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Raw (km, minutes) between the given road-graph nodes.

        Anything already known is read straight out of the table; anything new
        is computed, folded in, and will be free next time.
        """
        self.load()
        wanted = np.asarray(node_idx, dtype=np.int64)

        unseen = [int(n) for n in dict.fromkeys(wanted.tolist())
                  if int(n) not in self._position]
        if unseen:
            self._extend(graph, np.array(unseen, dtype=np.int64))

        rows = np.array([self._position[int(n)] for n in wanted], dtype=np.int64)
        km = self.km[np.ix_(rows, rows)].astype(float)
        minutes = self.minutes[np.ix_(rows, rows)].astype(float)
        # Two meters can snap to the same junction, and a location is zero
        # distance from itself whatever the table says.
        np.fill_diagonal(km, 0.0)
        np.fill_diagonal(minutes, 0.0)
        return km, minutes

    def _extend(self, graph, new_nodes: np.ndarray) -> None:
        """Work out the rows and columns for locations never seen before."""
        old = self.nodes
        n_old, n_new = len(old), len(new_nodes)
        started = time.time()
        log.debug(
            "%d new location(s) to measure against %d known one(s) "
            "(%d shortest-path searches%s)",
            n_new, n_old, 2 * n_new if n_old else n_new,
            "" if n_old else ", first run",
        )

        combined = np.concatenate([old, new_nodes])

        # Rows: every trip *out of* a new location, to old and new alike.
        row_km, row_min = graph.matrices_between(
            new_nodes, combined, weight=self.weight,
        )

        if n_old:
            # Columns: every trip *into* a new location. Searching the reversed
            # network answers all of them from one search per new location.
            back_km, back_min = graph.matrices_between(
                new_nodes, old, reverse=True, weight=self.weight,
            )
            col_km, col_min = back_km.T, back_min.T      # old -> new
        else:
            col_km = np.zeros((0, n_new), dtype=float)
            col_min = np.zeros((0, n_new), dtype=float)

        size = n_old + n_new
        km = np.zeros((size, size), dtype=np.float32)
        minutes = np.zeros((size, size), dtype=np.float32)
        if n_old:
            km[:n_old, :n_old] = self.km
            minutes[:n_old, :n_old] = self.minutes
            km[:n_old, n_old:] = col_km
            minutes[:n_old, n_old:] = col_min
        km[n_old:, :] = row_km
        minutes[n_old:, :] = row_min

        self.nodes = combined
        self.km, self.minutes = km, minutes
        self._position = {int(n): i for i, n in enumerate(combined)}
        self._dirty = True

        log.debug(
            "Travel matrix now covers %d locations (took %.1fs for the %d new "
            "one(s))", size, time.time() - started, n_new,
        )
        if size > _WARN_NODES:
            log.warning(
                "The travel matrix cache holds %d locations (%.0f MB). Meters "
                "that were repaired long ago are still in it; delete %s to "
                "start it again if it grows unwieldy.",
                size, (km.nbytes + minutes.nbytes) / 1e6, self.path,
            )
