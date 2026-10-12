"""Monitoring configuration: modules, schedules, and the documents agents run.

Phase 2. Four concerns, one per module, and the split is the one the product
needs rather than a tidy one:

* :mod:`hcc.monitoring.schedule` — when a module runs. One model shared by
  every module and every periodic job, so an operator learns it once
  (``docs/SCHEDULING.md``).
* :mod:`hcc.monitoring.modules` — what may be collected at all. A closed,
  versioned registry; a module identifier that is not in it is rejected rather
  than stored for a future agent to discover.
* :mod:`hcc.monitoring.canonical` — the one deterministic serialisation, and
  the checksum over it. Shared by the controller and the agent, because a
  checksum two programs compute differently is worse than no checksum.
* :mod:`hcc.monitoring.resolve` — global defaults plus per-host overrides into
  one effective document, with the provenance of every value preserved.

## Nothing here enables anything

Every module in the registry ships ``enabled=False`` and every resolution path
preserves that. Enrolling an agent does not start collection; an administrator
does, per module, per host (``docs/MONITORING_MODULES.md`` §2). The registry
carries a *default interval to use when enabled*, which is not the same thing
as a default to run — and the type system keeps them apart by never storing an
``enabled`` default anywhere but ``False``.
"""
