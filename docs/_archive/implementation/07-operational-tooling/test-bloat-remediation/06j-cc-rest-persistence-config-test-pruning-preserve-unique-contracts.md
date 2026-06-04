# 06j — cc-rest persistence/config test pruning (preserve unique contracts)

## Goal

Two command_center suites carry ORM-declaration echoes and repeated Pydantic-mechanism tests, but the verifier found a genuinely-unique contract buried in each that must survive. Prune the redundant echoes; keep the unique guards.

## Reading

* `tests/command_center/persistence/test_tables.py` — `TestAlertRow` / `TestWebauthnCredentialRow` / `TestOperatorSessionRow` column-set + PK tests (L41–106); the keep-tests `test_metadata_carries_exactly_the_three_owned_tables`, `test_credential_id_has_fk_to_webauthn_credentials`.
* `tests/command_center/persistence/test_session.py`, `test_codecs.py` — the insert/get + round-trip coverage that subsumes the PK-by-declaration echoes.
* `tests/command_center/test_config.py` — field-read tests (L43–115), `test_discord_webhook_url_env_present` (L151), the 4× `test_extra_field_rejected` repeats (L94/129/163/368), `test_loads_shipped_yaml`.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

`test_tables.py` **(verifier):**

* DROP the PK-by-declaration echoes (`test_*_id_is_primary_key`) — `test_session.py::test_can_insert_alert_row` proves the PK by `session.get(AlertRow, 'alert-001')`, and `test_codecs.py` round-trips column values.
* KEEP the exact column-**set** assertions (`cols == {…}`) — no other test asserts the set is exactly those, so an extra/renamed column would otherwise pass unnoticed. KEEP `test_metadata_carries_exactly_the_three_owned_tables` and `test_credential_id_has_fk_to_webauthn_credentials` (the FK test is NOT duplicated by `test_session.py`, which tests a different before_flush mechanism).

`test_config.py` **(verifier):**

* COLLAPSE the 4× `test_extra_field_rejected` repeats (L94/129/163/368) to one (identical Pydantic mechanism).
* MERGE — do **not** delete — the shipped-config value spot-checks (`cc_session`/`cc_csrf` cookie names, `relying_party_id == 'localhost'`, `duration_hours`, `ALPHAMIND_DISCORD_WEBHOOK`) into a single "shipped `config/*.yaml` carries expected fields" assertion. This is the ONLY assertion that the shipped YAML carries these values (a live regression guard — `bind.port` already moved 8080→8090); `test_loads_shipped_yaml` asserts only `isinstance` and does not subsume it.

## Acceptance criteria

- [ ] PK-by-declaration echoes dropped; the exact column-set assertions + the metadata + FK guards retained.
- [ ] The 4 `extra='forbid'` repeats collapsed to one; the shipped-config value spot-checks merged into a single retained assertion (not deleted).
- [ ] `coverage report` for `src/alphamind/command_center/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/command_center/persistence tests/command_center/test_config.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms the column-set, FK, and shipped-config-value assertions still present.