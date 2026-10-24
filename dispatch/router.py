"""The OR-Tools routing model.

With ``penalties`` every job is an optional visit (prize-collecting VRP);
without them every job is mandatory.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .config import FleetConfig, SolverConfig
from .distance import DistanceProvider

log = logging.getLogger(__name__)


@dataclass
class RouteStop:
    job_index: int        # index into the jobs frame passed to the caller
    leg_km: float         # distance travelled from the previous stop
    leg_minutes: float | None = None  # real driving time, when the backend knows it


@dataclass
class TeamRoute:
    team: int             # 1-based team number
    stops: list[RouteStop]
    # The drive back to the end depot (0 on a day with no drive back).
    return_km: float = 0.0
    return_minutes: float | None = None
    # On-site minutes per stop, carried so the route can report a working day
    # without the caller having to hold FleetConfig as well.
    service_minutes: float = 0.0
    # Which day of the plan this route belongs to (0 = the first).
    day: int = 0
    # Branch depots the route leaves from and returns to, when the Working
    # Day sheet names them. Empty for callers with a single fixed depot.
    start_depot: str = ""
    end_depot: str = ""

    @property
    def jobs_km(self) -> float:
        """Distance covered getting to the jobs, excluding the drive home."""
        return sum(s.leg_km for s in self.stops)

    @property
    def total_km(self) -> float:
        """Everything the team actually drives, including the return leg."""
        return self.jobs_km + self.return_km

    @property
    def drive_minutes(self) -> float | None:
        """Time behind the wheel, including the drive home.

        ``None`` when the backend supplied no driving times at all, which is
        the only case where a working day cannot be stated honestly.
        """
        legs = [s.leg_minutes for s in self.stops]
        if not legs:
            return 0.0
        if any(m is None for m in legs):
            return None
        return sum(legs) + (self.return_minutes or 0.0)

    @property
    def work_minutes(self) -> float | None:
        """The team's whole day: driving + on site + the drive back to base."""
        driving = self.drive_minutes
        if driving is None:
            return None
        return driving + self.service_minutes * len(self.stops)


class RoutingError(RuntimeError):
    """Raised when the solver cannot produce a feasible plan."""


class ImprovementTracker:
    """Decide when the search has stopped improving.

    ``should_stop`` is true once nothing has improved for ``patience_seconds``
    (after at least ``min_seconds``). A lower penalty always counts as an
    improvement; a shorter drive counts when it saves at least ``min_gain_pct``.
    """

    def __init__(self, patience_seconds: float = 30.0,
                 min_gain_pct: float = 0.5, min_seconds: float = 20.0,
                 clock=None):
        self.patience = float(patience_seconds)
        self.min_gain = float(min_gain_pct) / 100.0
        self.min_seconds = float(min_seconds)
        self._clock = clock or time.monotonic
        self.started = self._clock()
        self.curve: list[tuple[float, int]] = []      # (elapsed, objective)
        self._best: float | None = None
        self._last_gain_at = self.started
        self._best_penalty: float | None = None     # with travel given
        self._best_travel: float | None = None

    def record(self, objective: float, travel: float | None = None) -> None:
        """Note a solution the search has just found.

        ``travel`` is the driving part of ``objective``; the rest is penalty.
        """
        now = self._clock()
        self.curve.append((round(now - self.started, 2), int(objective)))
        if travel is not None:
            penalty, travel = float(objective) - float(travel), float(travel)
            if self._best_penalty is None or penalty < self._best_penalty:
                self._best_penalty, self._best_travel = penalty, travel
                self._last_gain_at = now
            elif penalty == self._best_penalty and travel < self._best_travel:
                if (self._best_travel - travel) / max(self._best_travel, 1.0) >= self.min_gain:
                    self._last_gain_at = now
                    self._best_travel = travel
            self._best = min(self._best, float(objective)) if self._best is not None \
                else float(objective)
            return
        if self._best is None:
            self._best, self._last_gain_at = float(objective), now
            return
        gain = (self._best - float(objective)) / max(abs(self._best), 1.0)
        if gain >= self.min_gain:
            self._last_gain_at = now
        # The best is always updated; only *meaningful* gains reset patience,
        # so a long drizzle of 0.01% improvements still counts as flat.
        self._best = min(self._best, float(objective))

    def should_stop(self) -> bool:
        """True once the search has gone quiet for long enough."""
        now = self._clock()
        if now - self.started < self.min_seconds:
            return False
        if self._best is None:              # nothing found yet: keep going
            return False
        return (now - self._last_gain_at) >= self.patience

    @property
    def elapsed(self) -> float:
        return self._clock() - self.started

    def summary(self) -> str:
        """The curve, short enough to read in a log line."""
        if not self.curve:
            return "no solution recorded"
        first, last = self.curve[0], self.curve[-1]
        drop = (
            100.0 * (first[1] - last[1]) / abs(first[1]) if first[1] else 0.0
        )
        return (
            f"{len(self.curve)} improvements, {first[1]:,} at {first[0]:.0f}s "
            f"-> {last[1]:,} at {last[0]:.0f}s ({drop:.1f}% better)"
        )


def pool_cost_matrix(lats, lons, provider, cost_metric: str, solver_cfg) -> np.ndarray:
    """The jobs' arc costs in the unit ``solve_routes`` charges: seconds of
    driving for ``time``, scaled metres for ``distance``. Used to rank filler
    meters by their distance from the must-do ones before the solve."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    if str(cost_metric).lower() == "distance":
        km = provider.matrix_km(lats, lons)
        return np.rint(np.asarray(km, dtype=float) * solver_cfg.distance_scale).astype(np.int64)
    minutes = provider.matrix_minutes(lats, lons)
    if minutes is None:
        km = provider.matrix_km(lats, lons)
        minutes = np.asarray(km, dtype=float)       # 60 km/h: a km a minute
    return np.rint(np.asarray(minutes, dtype=float) * 60.0).astype(np.int64)


def _extract_routes(
    routing, manager, solution, km, minutes, fleet: FleetConfig,
    n_jobs: int, n_vehicles: int,
) -> list[TeamRoute]:
    """Read one solved model back into ``TeamRoute`` objects."""
    routes: list[TeamRoute] = []
    for vehicle in range(n_vehicles):
        stops: list[RouteStop] = []
        index = routing.Start(vehicle)
        prev_node = manager.IndexToNode(index)
        while not routing.IsEnd(index):
            index = solution.Value(routing.NextVar(index))
            node = manager.IndexToNode(index)
            if 1 <= node <= n_jobs:
                stops.append(
                    RouteStop(
                        job_index=node - 1,
                        leg_km=float(km[prev_node, node]),
                        leg_minutes=(
                            None if minutes is None
                            else float(minutes[prev_node, node])
                        ),
                    )
                )
                prev_node = node

        # The drive back from the last job (0 to the free virtual end).
        return_km = 0.0
        return_minutes = None
        if stops:
            end_node = manager.IndexToNode(routing.End(vehicle))
            return_km = float(km[prev_node, end_node])
            if minutes is not None:
                return_minutes = float(minutes[prev_node, end_node])

        routes.append(
            TeamRoute(
                vehicle + 1, stops, return_km, return_minutes,
                service_minutes=float(fleet.service_minutes),
            )
        )
    return routes


def _pad_zero(matrix: np.ndarray) -> np.ndarray:
    """``matrix`` with one more row and column of zeros (a free virtual node)."""
    m = np.asarray(matrix)
    out = np.zeros((m.shape[0] + 1, m.shape[1] + 1), dtype=m.dtype)
    out[: m.shape[0], : m.shape[1]] = m
    return out


def solve_routes(
    lats: np.ndarray,
    lons: np.ndarray,
    depot_lat: float,
    depot_lon: float,
    provider: DistanceProvider,
    fleet: FleetConfig,
    solver_cfg: SolverConfig,
    num_vehicles: int | None = None,
    penalties: np.ndarray | None = None,
    cost_metric: str = "distance",
    time_limit_seconds: int | None = None,
    allowed_vehicles: list[list[int]] | None = None,
    vehicle_capacities: list[int] | None = None,
    vehicle_depots: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
) -> list[TeamRoute]:
    """Route ``len(lats)`` jobs across the vehicles.

    ``penalties``          one per job; makes every job optional at that cost
    ``cost_metric``        "time" (seconds) or "distance" (metres)
    ``allowed_vehicles``   per job, the vehicles that may serve it
    ``vehicle_capacities`` per vehicle job limit
    ``vehicle_depots``     per vehicle (start, end); an end of None means no drive back
    """
    n_vehicles = num_vehicles or fleet.num_teams
    n_jobs = len(lats)
    if n_jobs == 0:
        return [TeamRoute(team=v + 1, stops=[]) for v in range(n_vehicles)]

    if vehicle_capacities is not None and len(vehicle_capacities) != n_vehicles:
        raise ValueError(
            f"vehicle_capacities has {len(vehicle_capacities)} entries for "
            f"{n_vehicles} vehicles"
        )
    # Branch depots beyond the default one become extra nodes after the jobs.
    home = (float(depot_lat), float(depot_lon))
    extra_depots: list[tuple[float, float]] = []
    if vehicle_depots is not None:
        if len(vehicle_depots) != n_vehicles:
            raise ValueError(
                f"vehicle_depots has {len(vehicle_depots)} entries for "
                f"{n_vehicles} vehicles"
            )
        for pair in vehicle_depots:
            for point in pair:
                if point is None:                # no drive back
                    continue
                p = (float(point[0]), float(point[1]))
                if p != home and p not in extra_depots:
                    extra_depots.append(p)

    def depot_node(point) -> int:
        p = (float(point[0]), float(point[1]))
        return 0 if p == home else n_jobs + 1 + extra_depots.index(p)

    all_lats = np.concatenate((
        [depot_lat], np.asarray(lats, dtype=float), [p[0] for p in extra_depots],
    ))
    all_lons = np.concatenate((
        [depot_lon], np.asarray(lons, dtype=float), [p[1] for p in extra_depots],
    ))
    km = provider.matrix_km(all_lats, all_lons)
    # Real road driving times, used for the arrival-time column.
    minutes = provider.matrix_minutes(all_lats, all_lons)

    # Integer cost matrix with an optional zero-cost virtual end node.
    metric = str(cost_metric).strip().lower()
    if metric not in ("time", "distance"):
        raise ValueError(
            f"cost_metric must be 'time' or 'distance', got {cost_metric!r}"
        )
    if metric == "time":
        if minutes is None:
            log.warning(
                "Backend gave no driving times; deriving them from distance "
                "at %.0f km/h so arcs can still be costed in time.",
                fleet.avg_speed_kmh,
            )
            minutes = km / max(fleet.avg_speed_kmh, 1e-9) * 60.0
        # Seconds. No scaling: the objective then reads directly as seconds of
        # driving, which is what makes the penalty bands easy to reason about.
        cost = np.rint(minutes * 60.0).astype(np.int64)
    else:
        scale = solver_cfg.distance_scale
        cost = np.rint(km * scale).astype(np.int64)
    n_nodes = n_jobs + 1 + len(extra_depots)
    if vehicle_depots is not None:
        if any(e is None for _, e in vehicle_depots):
            # One virtual end, reached for free from anywhere: a vehicle
            # that ends there stops at its last job.
            open_end = n_nodes
            n_nodes += 1
            cost = _pad_zero(cost)
            km = _pad_zero(km)
            minutes = None if minutes is None else _pad_zero(minutes)
        starts = [depot_node(s) for s, _ in vehicle_depots]
        ends = [open_end if e is None else depot_node(e)
                for _, e in vehicle_depots]
    else:
        starts = [0] * n_vehicles
        ends = [0] * n_vehicles

    # A crew does at most jobs_per_team meters a day, or what the Working Day
    # sheet gives each (day, team).
    if vehicle_capacities is not None:
        caps = [max(int(c), 0) for c in vehicle_capacities]
    else:
        caps = [fleet.jobs_per_team] * n_vehicles

    def attempt(first: str, meta: str, seconds: int):
        """Build and solve the model with one search strategy."""
        manager = pywrapcp.RoutingIndexManager(n_nodes, n_vehicles, starts, ends)
        routing = pywrapcp.RoutingModel(manager)

        def distance_cb(from_index: int, to_index: int) -> int:
            return int(
                cost[manager.IndexToNode(from_index), manager.IndexToNode(to_index)]
            )

        transit = routing.RegisterTransitCallback(distance_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(transit)

        def demand_cb(from_index: int) -> int:
            node = manager.IndexToNode(from_index)
            return 1 if 1 <= node <= n_jobs else 0

        demand = routing.RegisterUnaryTransitCallback(demand_cb)
        routing.AddDimensionWithVehicleCapacity(
            demand, 0, caps, True, "stops"
        )

        # Optional visits: one disjunction per job, skipped at its penalty.
        if penalties is not None:
            for job in range(n_jobs):
                routing.AddDisjunction(
                    [manager.NodeToIndex(job + 1)], int(penalties[job])
                )

        # Which (day, team) vehicles may serve each job. -1 stays in the set
        # because it means "not visited"; without it the job becomes mandatory.
        if allowed_vehicles is not None:
            for job, vehicles in enumerate(allowed_vehicles):
                if vehicles is None or len(vehicles) >= n_vehicles:
                    continue                      # no restriction to apply
                if not vehicles:
                    raise RoutingError(
                        f"Job {job} is allowed on no vehicle at all; a job must "
                        f"either have somewhere to go or be left out of the pool"
                    )
                index = manager.NodeToIndex(job + 1)
                routing.VehicleVar(index).SetValues([-1] + [int(v) for v in vehicles])

        params = pywrapcp.DefaultRoutingSearchParameters()
        params.first_solution_strategy = getattr(
            routing_enums_pb2.FirstSolutionStrategy, first
        )
        params.local_search_metaheuristic = getattr(
            routing_enums_pb2.LocalSearchMetaheuristic, meta
        )
        # The time limit is the ceiling, not the plan: with stop_when_flat on,
        # the search normally ends before this because it stopped improving.
        params.time_limit.FromSeconds(seconds)

        tracker = None
        if solver_cfg.stop_when_flat or solver_cfg.log_improvement_curve:
            tracker = ImprovementTracker(
                patience_seconds=solver_cfg.flat_patience_seconds,
                min_gain_pct=solver_cfg.flat_min_gain_pct,
                min_seconds=solver_cfg.min_search_seconds,
            )
            # Called on each better solution; the driving part is summed along its routes.
            def travel_now() -> int:
                total = 0
                for v in range(n_vehicles):
                    i = routing.Start(v)
                    while not routing.IsEnd(i):
                        j = routing.NextVar(i).Value()
                        total += int(cost[manager.IndexToNode(i), manager.IndexToNode(j)])
                        i = j
                return total

            routing.AddAtSolutionCallback(
                lambda: tracker.record(routing.CostVar().Max(), travel_now())
            )

        stopped_early = False
        if tracker is not None and solver_cfg.stop_when_flat:
            # CustomLimit in OR-Tools 9.x, MakeCustomLimit in older builds.
            # If neither exists, only the time limit applies.
            try:
                solver = routing.solver()
                make = getattr(solver, "CustomLimit", None) or solver.MakeCustomLimit
                limit = make(tracker.should_stop)
                routing.AddSearchMonitor(limit)
                stopped_early = True
            except Exception as exc:                # noqa: BLE001
                log.warning(
                    "This OR-Tools build has no custom search limit (%s); "
                    "falling back to the %ds time limit. The convergence curve "
                    "is still recorded.", exc, seconds,
                )

        # Pace the progress bar on the usual search time, not the ceiling.
        from .progress import report
        typical = (
            solver_cfg.min_search_seconds + 2 * solver_cfg.flat_patience_seconds
            if solver_cfg.stop_when_flat else seconds
        )
        report(30, "ค้นหาเส้นทางที่ดีที่สุด", until=80,
               seconds=min(int(typical), int(seconds)))
        solution = routing.SolveWithParameters(params)
        report(80, "ได้เส้นทางแล้ว")

        if tracker is not None:
            if solver_cfg.log_improvement_curve:
                log.debug("  search: %s", tracker.summary())
            if stopped_early and tracker.elapsed < seconds - 1:
                log.debug(
                    "  stopped at %.0fs of the %ds ceiling: no gain above "
                    "%.2f%% for %.0fs",
                    tracker.elapsed, seconds, solver_cfg.flat_min_gain_pct,
                    solver_cfg.flat_patience_seconds,
                )
            elif stopped_early:
                log.debug(
                    "  ran the full %ds and was still improving - the ceiling "
                    "bound this solve, not the flatness rule", seconds,
                )

        if solution is None:
            return None
        return _extract_routes(
            routing, manager, solution, km, minutes, fleet, n_jobs, n_vehicles,
        )

    limit = time_limit_seconds or solver_cfg.time_limit_seconds
    restricted = (
        0 if allowed_vehicles is None
        else sum(1 for v in allowed_vehicles
                 if v is not None and 0 < len(v) < n_vehicles)
    )
    log.debug(
        "Solving %s: %d %s, %d routes, cap %s stops each, arcs costed in %s, "
        "%ds limit%s",
        "prize-collecting VRP" if penalties is not None else "VRP",
        n_jobs, "candidate meters" if penalties is not None else "jobs",
        n_vehicles,
        caps[0] if len(set(caps)) == 1 else f"{min(caps)}-{max(caps)} ({sum(caps)} in all)",
        metric, limit,
        f", {restricted} meters restricted to particular days" if restricted else "",
    )
    routes = attempt("PATH_CHEAPEST_ARC", "GUIDED_LOCAL_SEARCH", limit)
    if routes is None:
        raise RoutingError("OR-Tools found no feasible routing solution")

    if penalties is not None:
        taken = sum(len(r.stops) for r in routes)
        log.debug(
            "Solver selected %d of %d candidate meters (%d left for another "
            "day)", taken, n_jobs, n_jobs - taken,
        )

    jobs_km = sum(r.jobs_km for r in routes)
    total_km = sum(r.total_km for r in routes)
    log.debug(
        "Solution: %.1f km to the jobs + %.1f km returning to base "
        "= %.1f km total, per-team stops %s",
        jobs_km, total_km - jobs_km, total_km, [len(r.stops) for r in routes],
    )
    return routes
