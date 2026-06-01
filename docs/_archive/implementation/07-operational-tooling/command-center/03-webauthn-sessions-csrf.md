# 03 — WebAuthn + sessions + CSRF

## Goal

Ship the authentication layer: WebAuthn (passkey) registration + login via `py_webauthn`, signed-cookie session management with CSRF protection on state-mutating endpoints, and the `current_session` FastAPI dependency that all subsequent `/api/*` routes gate behind. WebAuthn calls are wrapped behind a `WebauthnVerifier` Protocol so unit tests use an in-memory fake; real verification runs only in the verify script. Single-operator model — no multi-user provisioning, no roles.

## Reading

* `docs/design/command-center.md` § Authentication and access — identity model, WebAuthn approach, session model, CSRF discipline.
* `py_webauthn` library current docs — `generate_registration_options`, `verify_registration_response`, `generate_authentication_options`, `verify_authentication_response` shapes. Pin the API surface this story uses behind the `WebauthnVerifier` Protocol so a library upgrade doesn't ripple through the routes.
* `src/alphamind/command_center/persistence/tables.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `webauthn_credentials` and `operator_sessions` tables this story populates.
* `src/alphamind/command_center/_kernel/ids.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `OperatorSessionId`, `WebauthnCredentialId` NewTypes.
* `src/alphamind/command_center/config.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — `SecurityConfig` (session duration, cookie names, WebAuthn relying-party).
* `src/alphamind/command_center/app.py` (from [ALP-666](https://linear.app/alphamind-jatassi/issue/ALP-666/02-command-center-backend-foundation)) — where to register the new auth router.

## Depends on

* [ALP-666](<https://linear.app/alphamind-jatassi/issue/ALP-666>) (02, this work tree) — credential / session tables, SecurityConfig, FastAPI app.

## Scope

In scope, under `src/alphamind/command_center/auth/`. Tests at `tests/command_center/auth/`.

### 1\. New `command_center/auth/` subpackage

```
src/alphamind/command_center/auth/
├── __init__.py
├── webauthn.py             # WebauthnVerifier Protocol + RealWebauthnVerifier wrapping py_webauthn
├── sessions.py             # signed-cookie session encode/decode, CSRF token mint+verify, expiry checks
├── routes.py               # /auth/register/begin, /auth/register/complete, /auth/login/begin, /auth/login/complete, /auth/logout
└── dependencies.py         # current_session FastAPI dependency + csrf_required dependency
```

### 2\. `WebauthnVerifier` Protocol (`webauthn.py`)

```python
class WebauthnVerifier(Protocol):
    def generate_registration_options(
        self, *, user_id: bytes, user_name: str, existing_credentials: Sequence[WebauthnCredentialId]
    ) -> RegistrationOptions: ...
  
    def verify_registration_response(
        self, *, options: RegistrationOptions, response: RegistrationResponseJson
    ) -> VerifiedRegistration: ...
  
    def generate_authentication_options(
        self, *, allow_credentials: Sequence[WebauthnCredentialId]
    ) -> AuthenticationOptions: ...
  
    def verify_authentication_response(
        self, *, options: AuthenticationOptions, response: AuthenticationResponseJson,
        stored_public_key: bytes, stored_sign_count: int
    ) -> VerifiedAuthentication: ...
```

`RealWebauthnVerifier` wraps the four `py_webauthn` calls. `FakeWebauthnVerifier` (in `tests/command_center/auth/fakes.py`) returns canned challenges and accepts any matching response.

### 3\. Session management (`sessions.py`)

Signed-cookie session encoding using `itsdangerous` (already a transitive FastAPI dep) or `starlette`'s built-in signing primitive. Session payload: `{session_id: OperatorSessionId, expires_at: datetime}`. The session row in `operator_sessions` is the authoritative state; the cookie is just a presented bearer for it. CSRF: double-submit cookie pattern — a `cc_csrf` cookie (not HttpOnly) + `X-CSRF-Token` header on state-mutating requests; the header value must match the cookie's value (verified server-side).

### 4\. Routes (`routes.py`)

Five endpoints:

* `POST /auth/register/begin` — gated by an out-of-band setup token shown on the local console at first launch (the setup token is consumed once; subsequent registrations require an existing-session bearer). Returns `RegistrationOptions` JSON for the browser's `navigator.credentials.create()` call.
* `POST /auth/register/complete` — accepts the `RegistrationResponseJson`; verifies via `WebauthnVerifier`; persists the credential to `webauthn_credentials`; creates an `operator_sessions` row + sets cookies.
* `POST /auth/login/begin` — returns `AuthenticationOptions` JSON for `navigator.credentials.get()`. Includes `allow_credentials` listing the registered `credential_id`s.
* `POST /auth/login/complete` — accepts the `AuthenticationResponseJson`; verifies; creates an `operator_sessions` row + sets cookies; updates the credential's `sign_count`.
* `POST /auth/logout` — deletes the session row + clears cookies. CSRF-required.

### 5\. Dependencies (`dependencies.py`)

```python
async def current_session(
    cc_session: Annotated[str | None, Cookie()] = None,
    cc_writer: Annotated[AsyncSession, Depends(cc_writer_session)] = ...
) -> OperatorSessionId:
    """Returns the validated OperatorSessionId; raises 401 if no/invalid/expired session."""

async def csrf_required(
    request: Request,
    cc_csrf: Annotated[str | None, Cookie()] = None,
) -> None:
    """Raises 403 if the X-CSRF-Token header doesn't match the cc_csrf cookie."""
```

Every `/api/*` router added by later stories must include `Depends(current_session)`; every state-mutating route (POST/PUT/PATCH/DELETE) must include `Depends(csrf_required)`. Document this contract in `auth/__init__.py` so subsequent stories find it.

### 6\. Setup-token flow

First-launch bootstrap: when no credentials exist in the DB, the command center prints a setup token to stdout (NSSM captures it in the log file). The operator presents this token to `/auth/register/begin` once. After the first credential lands, the setup-token path is locked out.

### Out of scope

* Frontend WebAuthn UI (registration + login HTML/TS) — story 04c.
* Multi-user provisioning, roles, or per-user audit segregation.
* Session-on-rotation, key rotation, or back-up codes — single-operator + add-passkey for fallback.

## Acceptance criteria

- [ ] `command_center/auth/{__init__,webauthn,sessions,routes,dependencies}.py` exist with the documented surfaces.
- [ ] `WebauthnVerifier` Protocol exists; `RealWebauthnVerifier` wraps `py_webauthn`; `FakeWebauthnVerifier` exists under `tests/command_center/auth/fakes.py`.
- [ ] `/auth/register/begin` and `/auth/register/complete` round-trip a new credential via the fake; the credential persists to `webauthn_credentials`.
- [ ] `/auth/login/begin` and `/auth/login/complete` round-trip a session via the fake; an `operator_sessions` row appears + cookies are set with `HttpOnly`, `SameSite=Strict`, `Secure` flags.
- [ ] `/auth/logout` deletes the session row and clears cookies.
- [ ] `current_session` raises 401 when the cookie is absent / signature-invalid / session expired.
- [ ] `csrf_required` raises 403 when the `X-CSRF-Token` header is absent or doesn't match `cc_csrf`.
- [ ] Setup-token flow: first call to `/auth/register/begin` accepts a token-only auth; subsequent calls require an existing-session bearer.
- [ ] Tests under `tests/command_center/auth/` cover: registration roundtrip, login roundtrip, session expiry, CSRF mismatch, setup-token consumption.
- [ ] `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run lint-imports` all pass.

## Verification

Scoped pytest: `uv run pytest tests/command_center/auth/ -n auto`. Manual smoke deferred to the verify story 07 (real passkey roundtrip requires a real authenticator).