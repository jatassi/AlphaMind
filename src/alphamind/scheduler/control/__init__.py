"""Pipeline ``/control`` + ``/events`` HTTP surface.

Loopback-bound FastAPI + Uvicorn server hosted alongside APScheduler in
the pipeline-scheduler process.  Exposes the five ``POST /control/*``
verbs (pause, resume, trigger_emergency_invocation, switch_profile,
run_universe_validation) and the ``GET /events`` SSE stream specified in
``docs/design/pipeline-control-and-events-schema.md``.

The browser-facing ``/api/control/*`` proxy on the command-center
backend (story 04a) is the only client; loopback isolation is the trust
boundary and this surface carries no authentication.

Internal package layout per the python-architecture brief: Pydantic at
boundaries only (``models.py``); pure-ish verb implementations over
injected Protocols (``verbs.py``); SSE emitter + per-event records
(``events.py``); route handlers (``routes.py``); FastAPI app + lifespan +
Uvicorn supervisor task (``app.py``).
"""
