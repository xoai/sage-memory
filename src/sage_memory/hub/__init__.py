"""sage-memory hub federation (M2 of the 0.13.0 cycle).

Per ADR-008: cross-project search + routed-write via a registered
hub of project DBs. Config lives at ``~/.sage-hub.yaml``. This
package provides:

  - ``hub.config`` — load/save the YAML registry + schema-evolution
  - ``hub.search`` — fan-out search across registered projects (M2.3)
  - ``hub.store``  — routed-write to a named project (M3.3)
  - ``hub.importer`` — copy an existing DB into a hub project (M3.4)
  - ``hub.ownership`` — PID + heartbeat writer-discipline (M3.1)
"""
