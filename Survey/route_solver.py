import math
from typing import List, Tuple


def nearest_neighbor_route(
    locations: List[Tuple[float, float]],
    start: Tuple[float, float],
) -> List[int]:
    """Nearest-neighbor TSP starting from `start`.
    Returns ordered list of indices into `locations`."""
    if not locations:
        return []

    n = len(locations)
    visited = [False] * n
    route = []
    current = start

    for _ in range(n):
        best_dist = float("inf")
        best_idx = -1
        for i in range(n):
            if visited[i]:
                continue
            dx = locations[i][0] - current[0]
            dy = locations[i][1] - current[1]
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < best_dist:
                best_dist = dist
                best_idx = i
        if best_idx == -1:
            break
        visited[best_idx] = True
        route.append(best_idx)
        current = locations[best_idx]

    # 2-opt improvement pass (includes distance from start)
    route = two_opt_improve(locations, route, start)
    # Or-opt (node relocation) pass: moves individual stops to better positions.
    # This fixes "should have visited nearby cluster node first" cases that
    # 2-opt misses because they require relocating a single point, not reversing
    # a segment.
    route = or_opt_improve(locations, route, start)
    # One final 2-opt pass to consolidate any gains from Or-opt.
    route = two_opt_improve(locations, route, start)
    return route


def _dist(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


def _route_total(locations: List[Tuple[float, float]], route: List[int],
                 start: Tuple[float, float]) -> float:
    """Total distance including start → first stop."""
    if not route:
        return 0.0
    total = _dist(start, locations[route[0]])
    for i in range(len(route) - 1):
        total += _dist(locations[route[i]], locations[route[i + 1]])
    return total


def two_opt_improve(
    locations: List[Tuple[float, float]], route: List[int],
    start: Tuple[float, float],
) -> List[int]:
    """2-opt improvement. All positions including the first stop can be swapped."""
    if len(route) <= 2:
        return route

    route = list(route)
    best_dist = _route_total(locations, route, start)
    improved = True
    while improved:
        improved = False
        for i in range(len(route) - 1):
            for j in range(i + 1, len(route)):
                new_route = route[:i] + route[i:j + 1][::-1] + route[j + 1:]
                new_dist = _route_total(locations, new_route, start)
                if new_dist < best_dist - 0.01:
                    route = new_route
                    best_dist = new_dist
                    improved = True
    return route


def or_opt_improve(
    locations: List[Tuple[float, float]], route: List[int],
    start: Tuple[float, float],
) -> List[int]:
    """Or-opt (node relocation) improvement.

    For each stop in the route, try removing it and reinserting it at every
    other position.  Accepts the move if the total route distance decreases.

    This complements 2-opt: 2-opt reverses segments (good for untangling
    crossing paths), while Or-opt relocates single nodes (good for fixing
    "should have visited this nearby cluster node earlier" cases).
    """
    if len(route) <= 2:
        return route

    route = list(route)
    best_dist = _route_total(locations, route, start)
    improved = True
    while improved:
        improved = False
        for i in range(len(route)):
            node = route[i]
            # Route without this node
            remaining = route[:i] + route[i + 1:]
            for j in range(len(remaining) + 1):
                # Don't bother re-inserting at the original position
                if j == i:
                    continue
                candidate = remaining[:j] + [node] + remaining[j:]
                new_dist = _route_total(locations, candidate, start)
                if new_dist < best_dist - 0.01:
                    route = candidate
                    best_dist = new_dist
                    improved = True
                    break  # restart outer loop with updated route
            if improved:
                break
    return route
