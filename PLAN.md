# Strategic Plan: Optimize Nornir Filtering and Enable Batch Processing

## Objective

Enable filtered device discovery and batch task execution for Nornir-based MCP tools.

## Discovery Report

Current tools only support single-device filtering via `device_name`.

## Strategic Plan

1.  **Update `nornir_list_inventory`**: Add optional `group` and `platform` filters.
2.  **Update Task Tools**: Modify `nornir_get_facts`, `nornir_run_getter`, `nornir_get_config`, and `nornir_run_cli` to accept `devices: str | list[str]`.
3.  **Refactor `runner.py`**:
    - Implement filtering logic within `_resolve_filter` (replacing `_resolve_device`).
    - Add aggregation logic to `_run_getter`/`_run_cli` to process multiple hosts.
4.  **Verification**: Update existing tests and add new tests for filtering and batch execution.

## Assumptions & Risks

- **Assumption**: Batch execution requires result aggregation (returning a `dict` keyed by `device_name`).
- **Risk**: Modifying tool signatures might require adjustments in client-side expectations (though new signatures will remain backward compatible by supporting `str`).

## Proposed Changes

- `/home/zulu/Documents/net-tool/server.py`: Update tool definitions.
- `/home/zulu/Documents/net-tool/runner.py`: Update internal logic.
- `/home/zulu/Documents/net-tool/tests/test_helpers.py`: Update tests.

## Verification Pyramid

- Static: `mypy` and `ruff`.
- Unit: Existing tests pass, new tests for filtering and batch execution.
