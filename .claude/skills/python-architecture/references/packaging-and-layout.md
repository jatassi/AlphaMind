# Packaging and layout

How the project's physical shape is organised — directories, files, dependency declarations. Mostly conservative, mainstream practice; the cost of getting any of these wrong is paid forever, the cost of getting them right is paid once.

---

## §B1. `src/` layout for anything that is, or might become, a distribution

**Claim.** Use `src/<package>/` for any project you'd publish to PyPI, install editably across machines, or import from another project. Flat layout (`<package>/` at the repo root) is acceptable only for one-file scripts with no `pyproject.toml`.

**Rationale.** `src/` forces editable installs to behave like real installs. Without it, `import mypackage` works in development because the package directory is at the repo root and Python's path picks it up implicitly — but the test environment ends up importing development files even when you `pip install` something. With `src/`, the import only works if the package is actually installed, catching path-shape bugs at development time instead of in CI or production.

**Attribution.** Hynek Schlawack ("Testing & Packaging" series, hynek.me). Brett Cannon. The Python Packaging Authority's own guides recommend it. PEP 517 / 518 don't mandate it but most modern build backends (hatch, pdm, poetry) default to it.

**When it breaks.** Single-file scripts that don't need to be a package. There `src/` is overhead.

**Audit signal.** A `pyproject.toml` declaring a project, but no `src/` directory — package files at the repo root. Migration is a half-day task and should be in any meaningful audit's punch list.

**Design signal.** Start with `src/`. Cost is zero, retrofit is annoying.

---

## §B2. `pyproject.toml` is the only build / metadata file (PEP 621)

**Claim.** No `setup.py`, no `setup.cfg`, no `requirements.txt` checked in alongside. Tool config (ruff, mypy, pytest, coverage) goes under `[tool.*]` — one source of truth for everything the project tells its tools.

**Rationale.** Before PEP 621 (2020), Python had three or four overlapping ways to declare project metadata. After, there's one. Tools have caught up; modern toolchains (uv, hatch, pdm, poetry) all read `pyproject.toml`. Keeping legacy files alongside means duplication, drift, and confusion about which file is authoritative.

**Attribution.** PEP 621 (Brett Cannon et al.). Pradyun Gedam (PyPA).

**When it breaks.** Legacy build steps that need a `setup.py` shim (rare in 2026). Some niche distribution flows (manylinux wheels, editable installs of C extensions) may still need a thin shim.

**Audit signal.** Both `setup.py`/`setup.cfg` and `pyproject.toml` present. `requirements.txt` checked in alongside a lockfile. Tool config scattered across `.flake8`, `.coveragerc`, `mypy.ini`, `pytest.ini` instead of consolidated in `pyproject.toml`.

**Design signal.** New project: `pyproject.toml` only. Migrate any old project to consolidate as a clean-up pass.

---

## §B3. Package by feature, not by layer

**Claim.** Organise the source tree by feature / domain (`alphamind/orders/`, `alphamind/risk/`) and keep architectural layers (`domain.py`, `adapters.py`, `api.py`) as files *inside* each feature. Reject the alternative of layer-organised trees (`alphamind/models/`, `alphamind/services/`, `alphamind/routers/`).

**Rationale.** A feature is the unit of change. Bug reports, new requirements, code reviews, code ownership — they all map to features, not layers. A feature tree localises change to one directory; a layered tree forces every change to span every directory. Cosmic Python's Appendix B specifies this layout for the same reason. Django is the prominent counter-example because its framework imposes layer organisation; live with the framework's choices, but apply package-by-feature within each Django app.

**Attribution.** Cosmic Python Appendix B. Brandon Rhodes' "vertical slice" framing. Vaughn Vernon (DDD) on bounded contexts as packages.

**When it breaks.** Genuinely shared cross-feature code (a `User` entity used everywhere). Then a `shared/` or `core/` package below the features is fine. Keep it small; if `shared/` grows faster than the features, the features aren't really independent.

**Audit signal.** Top-level packages named `models/`, `services/`, `controllers/`, `views/`, `repositories/`, or `serializers/` (outside of frameworks that mandate them). Imports where every feature has to touch every layer.

**Design signal.** Top-level packages are features. Inside each feature, files separate concerns (`domain.py`, `adapters.py`, `api.py`) without forcing subdirectories until file size or dependency mass justifies promotion.

---

## §B4. `__init__.py` policy: empty for apps, curated for libraries

**Claim.** For application code, keep `__init__.py` empty and require importers to say `from alphamind.orders.domain import Order`. For library code with a stable public surface, curate `__init__.py` with explicit re-exports and a populated `__all__`.

**Rationale.** Empty `__init__.py` makes the dependency self-documenting (the import literally names the file the symbol lives in) and avoids circular-import landmines (re-exports trigger imports of submodules at package import time). Curated `__init__.py` for libraries trades the cost of maintaining the re-export list for the ergonomic win of `from httpx import Client` instead of `from httpx._client import Client`. The Requests library is the cautionary tale: the heavy `__init__.py` re-export web makes every internal change a potential breakage.

**Attribution.** Hynek Schlawack on packaging hygiene. Glyph Lefkowitz on the costs of import-time work.

**When it breaks.** Application packages with a small, stable, top-level API used by many external consumers. There a curated `__init__.py` with re-exports can pay off. The bar is high; most application code shouldn't.

**Audit signal.** Library `__init__.py` files that re-export everything via `from .module import *`. Application `__init__.py` files that re-export anything at all.

**Design signal.** New application: empty `__init__.py`. New library: `__init__.py` re-exports the documented public API only, with `__all__`.

---

## §B5. `uv` for new projects; `uv` or Poetry for existing

**Claim.** Use `uv` for new projects. It replaces pip + pip-tools + virtualenv + pyenv + most of Poetry, is 10–100× faster, and produces a `uv.lock` reproducible across platforms. Existing Poetry projects can stay on Poetry; greenfield should be `uv`.

**Rationale.** The Astral toolchain has consolidated what was previously a fragmented ecosystem. The performance delta is large enough to change developer habits — `uv sync` and `uv run` make running tests in clean environments cheap. Poetry remains stable and widely-deployed; for established projects, the migration cost outweighs the speed win.

**Attribution.** Astral (Charlie Marsh's team) on uv. The Python Packaging Authority's gradual endorsement of multiple tools as long as they speak `pyproject.toml` + lockfile.

**When it breaks.** Niche distribution requirements (manylinux + C extensions, ABI-sensitive builds) where uv is still catching up. Migrate cautiously there.

**Audit signal.** `requirements.txt` + `pip` only, with no lockfile. `setup.py`-driven projects. Mixed dependency declarations across multiple files.

**Design signal.** New project: `uv init`, commit `uv.lock`. CI runs `uv sync --frozen`.

---

## §B6. Pin in apps, range in libraries

**Claim.** Apps (deployable services): commit a lockfile, pin everything transitively, `uv sync --frozen` in CI and prod. Libraries (published distributions): broad ranges (`httpx>=0.25,<1.0`) in `pyproject.toml`, no lockfile in the published artefact.

**Rationale.** Apps need to be reproducible; six months from now you want exactly the same dependency tree to debug a regression. Libraries need to be flexible; pinning forces every downstream consumer to match your exact tree, which guarantees conflict in any non-trivial app. The split is well-established and any reasonable packaging tool encodes it.

**Attribution.** Łukasz Langa (PEP 440 author). The Python Packaging Authority's app vs. library guides. Donald Stufft.

**When it breaks.** Libraries used only inside one organisation, deployed only with that organisation's apps — there pinning makes sense, treat them as apps.

**Audit signal.** A library with a lockfile committed and the lockfile referenced from `pyproject.toml`. An app without a lockfile.

**Design signal.** Apps lock; libraries range; document which the project is.

---

## §B7. Worktree or monorepo-of-one over polyrepo splits

**Claim.** For solo / small-team projects, keep everything in one repo until there's a clear external consumer. Splits are expensive to undo; the benefits of separate repos (independent release cadence, independent CI, narrower blast radius) are real but only matter at organisational scale.

**Rationale.** Splitting one project into multiple distributions adds publishing overhead, version-coordination overhead, and import-path overhead, in exchange for benefits that mostly accrue to teams of teams. For a single team, the in-repo equivalents (path-based dependencies, local editable installs, single CI) achieve the same isolation without the overhead.

**Attribution.** Pragmatic consensus across recent PyCon talks on monorepos. Pants and Bazel exist for the genuinely multi-distribution case.

**When it breaks.** When you genuinely have ≥2 external consumers of one component, or the release cadence of one component differs from the rest by an order of magnitude. Then splitting is justified.

**Audit signal.** Premature splits — multiple repos with one consumer each, or repos that are only ever released together. Conversely, a monorepo with three completely independent products that share nothing.

**Design signal.** Default to a single repo. Plan the split when you can name the second consumer.
