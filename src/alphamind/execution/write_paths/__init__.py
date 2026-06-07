"""fill collection and command execution write-path helpers.

The fill collection entry point ``process_unprocessed_fills`` lives in
``write_paths.fill_collection`` and is imported directly by callers — eager
re-export from this package would create a circular import with the
``tables.corporate_action_integration_ledger`` module, which depends on
``write_paths.records``.
"""
