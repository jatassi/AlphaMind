After the 2026-05-03 fix adding `break` to `_collect_response` after a successful `ResultMessage` (to avoid the SDK's post-result `Command failed exit 1` bug), the early loop exit leaves the SDK's internal async generator un-drained. On exit, the SDK's cleanup path emits `RuntimeError: aclose(): asynchronous generator is already running` to stderr.

Cosmetic only — verification still passes.

**Fix:** Either drain the generator explicitly (`async for _ in sdk_query_fn(...): pass` after the break-bearing loop completes) or call `await query.aclose()` on the iterator before returning.

Affects all four analysis-layer harnesses: domain researchers, qualitative researcher, adaptive researcher, synthesizer.