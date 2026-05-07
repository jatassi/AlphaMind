# Design questions

The clarifying questions to ask in design mode. Don't ask all of them every time — pick the 3–5 most load-bearing for the design at hand. The order matters: earlier answers change later questions.

---

## 1. What kind of system is this? (application shape)

Pick one or describe the closest analogue:
- **CLI** — invoked from the shell, exits when done.
- **Web API** — long-running, HTTP request-response.
- **Web app with UI** — server-rendered or SPA-backed.
- **Data pipeline / ETL** — stage-oriented, scheduled or event-driven.
- **Long-running daemon / service** — always-on, background work.
- **Agentic / LLM application** — tool-using, multi-turn, possibly autonomous.
- **Numerical / scientific** — pandas / numpy heavy, performance-sensitive.
- **Library / SDK** — published distribution, third-party consumers.
- **Mixed** — describe the mix.

**Why this matters.** Application shape changes nearly everything downstream — defaults, frameworks, testing seams, antipatterns. See `app-shapes.md`.

---

## 2. Where are the trust boundaries?

For each boundary, name what crosses it and in what direction:
- **Inbound:** HTTP requests? CLI args? Files? Vendor API responses? LLM tool outputs? Database reads of user-controlled data?
- **Outbound:** HTTP responses? File writes? DB writes? External API calls?

**Why this matters.** Each trust boundary is a Pydantic-or-validation point. The boundary types will be designed first; the internal types follow.

---

## 3. What state survives a restart, and where does it live?

- **In-memory state** — only for the lifetime of the process; lost on restart.
- **Local file** — JSON / CSV / SQLite / pickle on local disk.
- **Local DB** — SQLite or Postgres on the same machine.
- **Remote DB** — Postgres / MySQL / Mongo / cloud database.
- **Cache** — Redis / Memcached, fast but not durable.
- **External system of record** — a vendor system holds the truth; we cache.

**Why this matters.** Persistence engine, schema migration story (§K7), recovery semantics, and concurrency model (§G) all flow from this.

---

## 4. What's the concurrency profile?

Pick all that apply:
- **One thing at a time** — sequential, no concurrency.
- **Concurrent I/O** — multiple HTTP / DB / file operations overlapping.
- **CPU-bound parallel work** — numerical computation that fans out.
- **Long-running with background tasks** — main loop with supervised workers.
- **Many short invocations** — each invocation is small, but many overlap (web app).

**Why this matters.** Determines async vs. sync (§G1), structured concurrency requirements (§G2), and process model.

---

## 5. What's the failure model?

- **What can fail?** External APIs (timeouts, rate limits, errors), persistence (lock contention, disk full), invalid input, transient network issues, downstream services.
- **What's the cost of a failure?** Lose data? Lose money? Annoy a user? Trigger an alert?
- **What's the recovery model?** Retry? Fall back to default? Surface to user? Manual intervention?

**Why this matters.** Determines retry policies (§F4), idempotency requirements, backoff strategies, error propagation, alerting thresholds.

---

## 6. Who consumes this and how?

- **Just me, this codebase only** — internal module.
- **Other projects in this org** — internal library.
- **External developers** — public library / SDK.
- **End users via UI / CLI** — different ergonomics needed.
- **Other services** — API consumer.
- **The LLM** — tool / function definition.

**Why this matters.** Public API discipline (§C5), versioning, documentation, ergonomic constraints.

---

## 7. What constraints are already fixed?

- **Python version** — 3.13? 3.12? Some legacy 3.10?
- **Framework** — already on FastAPI? Django? Flask?
- **Persistence engine** — locked to SQLite? Postgres? Mongo?
- **Deployment target** — Docker? AWS Lambda? a Linux server with systemd? Windows with NSSM?
- **Existing libraries / patterns** — codebase conventions to maintain.
- **Team / org constraints** — code review process, deploy cadence, ops requirements.

**Why this matters.** Don't re-litigate fixed constraints. Design within them.

---

## 8. What's the deliverable from this design conversation?

- **A package layout sketch** — directory tree + key types.
- **A starter scaffold** — actual files that compile and run.
- **A migration plan** — for an existing system being restructured.
- **A discussion document** — for review with humans, not yet for implementation.

**Why this matters.** Different deliverables imply different levels of detail. A sketch can be loose; a starter scaffold needs working code.

---

## How many to ask

If the user has been specific about what they're building, you may need only 2–3 questions. If they've been vague ("design something that handles X"), you may need 4–5.

Ask in one go, not in series. Each question should be answerable in a sentence or two; the agent shouldn't drag the user through twenty rounds of one-question-at-a-time clarification.

If the user gives a partial answer, work with what you have. Don't keep asking until everything is specified — design is provisional, and the brief can flag the remaining unknowns.
