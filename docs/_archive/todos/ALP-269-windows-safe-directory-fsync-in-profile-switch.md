The directory-fsync block in `_rewrite_active_profile` (`src/alphamind/risk_guardrails/rules_and_limits/profile_switch.py:143`) calls `os.open(<dir>, os.O_RDONLY)` which raises `PermissionError [Errno 13]` on Windows because Win32 rejects directory handles via the POSIX `_open` shim.

**Failing tests on Windows production server:** `test_switch_to_different_profile_rewrites_main_yaml` and `test_rewrite_preserves_every_non_active_profile_field` in `tests/risk_guardrails/rules_and_limits/test_profile_switch.py`. Also fails if `/profile-switch` is ever invoked on Windows.

**Fix:** Guard with `if sys.platform != "win32":` — or drop the parent-dir fsync entirely. It is a Linux-only durability primitive; `os.replace` already gives the in-tree atomicity Windows can offer.