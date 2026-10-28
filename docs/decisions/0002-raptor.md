# 0002. Self-implemented RAPTOR over a compact, cached timetable

- Status: accepted
- Date: 2026-10-28

## Context
Every game round asks the same question many times: "which stations can be reached from X
(or from any of a set of stations) departing at T within 10 minutes, and how?". Bots ask it
more often, and with longer horizons: the greedy Dodger asks for 45 minutes from each Inspector,
and the belief tracker expands a whole set of candidate positions every round. Implementing the
router is also a learning goal, so an existing library (OpenTripPlanner, r5py) is out.

## Decision
Implement RAPTOR (Delling, Pajor, Werneck, 2012) in plain Python over a CSR-style
`Timetable` (flat int lists with offset arrays, stored as `.npz`):

- **Rounds = vehicles.** Round *k* scans only patterns serving a node improved in round
  *k−1*, from the earliest such position. It hops on the earliest catchable trip (binary
  search, valid because patterns are FIFO-split at build time) and switches to an earlier
  trip whenever a stop was reached early enough by other means.
- **Footpaths** are relaxed after each round from the nodes reached by vehicle. Walk → walk
  chains are not allowed, and an initial walk from the source is.
- **Change time** (60 s, or 120 s at metro stations) applies only between two vehicles at the
  same node. Boarding at the start, or after arriving on foot, is immediate.
- **Two labels per node** (arrived by vehicle / arrived on foot) with separate dominance. A
  vehicle arrival enables walking on, and a foot arrival enables boarding without change time,
  so neither dominates the other. That makes the earliest arrivals exact under these rules.
- **Extras** the game needs: multi-source queries (one call expands a belief set), a
  vehicle-mode bitmask (for mode reveals), a hard `max_time` bound, and journey reconstruction
  from per-round labels. On equal arrival time the journey with fewer vehicles wins.

The same semantics are encoded in a brute-force Dijkstra over (node, arrival-kind) states.
Hypothesis compares the two on the fixture network (150 random multi-source, mode-masked
queries), and a local test compares them on the real network.

## Consequences
- On the default network (434 nodes, 205 patterns, 5,401 trips), a 10-minute query from a
  random station takes **0.35 ms median, 0.84 ms p95**; a 45-minute query covering the whole
  network takes ~3.6 ms (`fare-dodger bench`, Windows laptop, CPython 3.12). That is far
  below the 200 ms target, so no numba/Cython was needed.
- Only earliest arrival is optimised. A Pareto set (arrival × transfers) or rRAPTOR
  (departure-time ranges) can be added later on the same data layout.
- Time-dependent walking, platform-level change times and pathways are out of scope.
