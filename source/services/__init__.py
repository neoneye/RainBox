"""Side-service catalogue and supervision (see
docs/superpowers/specs/2026-09-10-launcher-design.md).

`services.definitions` is data-only, standard-library-only, and is imported by
BOTH the launcher (`main.py`) and the core: it is the one place that says
which side services exist, where they live, and what they may be given.
`services.registry` is the core-side half (settings, the desired-state
snapshot, launcher status) and imports `db`; the launcher must never import it.
"""
