# 12a — Vendor Protocols + in-memory fakes for `tests/data_sources/`

## Goal

Replace the 261 `MagicMock`/`Mock` constructions in `tests/data_sources/` with Protocol-shaped fakes (one Protocol per vendor SDK, one in-memory fake implementing each Protocol). The audit's data_sources LB-4 names this textbook P8 "mock what you don't own" failure: SDK upgrades that rename kwargs silently pass mock-based tests but break production. The single existing `FakeRepo` in `tests/data_sources/treasury/test_auctions.py` shows the right pattern; this story propagates it.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — high-yield "Test substrate" row + data_sources LB-4
* `.claude/skills/python-architecture/references/testing.md` § J1, J2 — sociable tests; don't mock what you don't own
* `tests/data_sources/treasury/test_auctions.py` `FakeRepo` — the existing exemplar
* `src/alphamind/data_sources/polygon/client.py` — typical vendor wrapper to extract Protocol from
* Story 09c (<issue id="6395f086-7f7c-4715-8d07-d52039cb11e5">ALP-473</issue>) — `_common.py` split; rate limiter, retry, run-tracking are now separate Protocols-eligible modules

## Depends on

* 09c (<issue id="6395f086-7f7c-4715-8d07-d52039cb11e5">ALP-473</issue>) — `data_sources/_common.py` split makes the cross-cutting Protocols (rate limiter, retry policy) cleanly extractable

## Scope

In scope: define one `Protocol` per vendor adapter; one in-memory `Fake*` per Protocol in `tests/data_sources/_fakes/` (or `tests/_fakes/data_sources/`); replace `MagicMock`/`Mock` constructions across `tests/data_sources/`. Tests update.

### 1\. Define vendor Protocols

For each vendor in `src/alphamind/data_sources/`:

```python
# data_sources/polygon/_protocol.py
class PolygonAPI(Protocol):
    def list_aggs(self, ticker: str, ...) -> Sequence[Bar]: ...
    def list_dividends(self, ticker: str, ...) -> Sequence[Dividend]: ...
    # ... only the methods AlphaMind actually calls
```

Existing wrapper classes (`PolygonClient`, `FredClient`, etc.) implement the Protocol naturally — declare it explicitly so the type checker enforces.

### 2\. In-memory fakes

For each Protocol, write one fake under `tests/data_sources/_fakes/`:

```python
# tests/data_sources/_fakes/polygon.py
@dataclass
class FakePolygonAPI:
    aggs_by_ticker: dict[str, list[Bar]] = field(default_factory=dict)
    dividends_by_ticker: dict[str, list[Dividend]] = field(default_factory=dict)

    def list_aggs(self, ticker: str, ...) -> Sequence[Bar]:
        return self.aggs_by_ticker.get(ticker, [])
    # ...
```

Fakes are stateful, ergonomic, and own the data shape — tests construct them by passing in the data, not by `mock.return_value =`.

### 3\. Replace MagicMocks

Sweep `tests/data_sources/`:

* `tests/data_sources/polygon/test_polygon.py:116` `mock_rest = MagicMock(); mock_rest.get_aggs.return_value = [...]` → `client = FakePolygonAPI(aggs_by_ticker={"AAPL": [bar1, bar2]})`
* Similar replacement across every vendor test

### 4\. Verify the contract

For each vendor with a real production wrapper (`PolygonClient`, etc.), assert it implements the Protocol via `_: PolygonAPI = PolygonClient(...)` at the bottom of the module (a runtime check; or use `typing.Protocol`'s `runtime_checkable`). The type checker catches at edit time; the runtime check catches at import time.

### Out of scope

The audit's broader test-substrate concerns in other subdivisions (portfolio_state's `pytest.mark.anyio` cleanup is handled by 08a; distillation's `sqlite:///:memory:` removal is handled by 07's pure-compute split) are not in scope for this story.

## Acceptance criteria

- [ ] One `Protocol` per vendor adapter, in `src/alphamind/data_sources/<vendor>/_protocol.py` (or similar).
- [ ] Every production wrapper class implements its Protocol (verified via type assertion).
- [ ] `tests/data_sources/_fakes/` contains one fake per Protocol.
- [ ] Zero `MagicMock`/`Mock` constructions remain in `tests/data_sources/` (count drops from 261 to 0; `treasury/test_auctions.py`'s existing `FakeRepo` pattern is now universal).
- [ ] `uv run pytest -n auto tests/data_sources/` runs faster (no mock instantiation overhead) and is brittleness-free against SDK kwarg renames (verify with a synthetic vendor-SDK swap).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto` all pass.

## Verification

`grep -rn "MagicMock\|Mock(" tests/data_sources/` returns zero hits. Synthetic SDK upgrade test: rename a kwarg in the production wrapper; the production code fails type-check while the fake-based tests still pass — confirming the seam isolates production code from test code.