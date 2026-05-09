# Broker Adapter Verification Runbook

Operator workflow for the broker-adapter work tree (ALP-121).

## Status

This runbook is a stub created in story 01 (ALP-378). Story 05 (ALP-393)
will populate it with the full verification workflow once the adapter's
order-translation, fill-stream, and venue-configuration components ship.

## Prerequisites

_To be populated by story 05 (ALP-393)._ Will document:

- Alpaca paper credentials in `.env` (`ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`).
- Operator-facing setup steps for the broker adapter.
- Pre-flight checks before executing the verification script.

## What to expect

The story 01 skeleton does not yet support a runnable verification script.
The substrate it ships (`AlpacaClientFactory`, `submit_with_retry`,
`classify_alpaca_error`) is exercised exclusively by the in-process pytest
suite at `tests/execution/broker_adapter/`. Run:

```bash
uv run pytest tests/execution/broker_adapter -n auto
```

to exercise the substrate.

Story 05 (ALP-393) will replace the contents of this runbook with the
end-to-end verify workflow that exercises each venue-configuration module
against live Alpaca paper endpoints.
