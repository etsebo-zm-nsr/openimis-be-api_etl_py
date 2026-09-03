# openIMIS Backend API ETL module

`api_etl` ingests records from external systems into openIMIS individuals and households.

This is the Zambia Social Registry fork
([etsebo-zm-nsr](https://github.com/etsebo-zm-nsr)) of `openimis/openimis-be-api_etl_py`.
It is the **shared core** of a plugin architecture: each external system is its own
connector module (`zm_etl_zispis`, `zm_etl_kobo`, …) that registers itself here, so
adding a source is a new repo plus one `openimis.json` line rather than a change to
shared code.

## What lives here

| Area | Responsibility |
|---|---|
| `registry.py` | connectors register themselves; the extension point |
| `config.py` | `SourceConfig`, deep-merged per-source configuration, `env:` secrets |
| `sources/base_http_source.py` | session, retries, timeouts, TLS/CA bundle, both paginators, cursor tracking |
| `adapters/base_mapping_adapter.py` | field mapping, dates, **national-ID normalisation**, role/household emission |
| `sinks/individual_import_sink.py` | the import workflow, **record linkage**, provenance, household grouping |
| `models.py` | `ETLSyncState` — cursor, lease, run outcome |
| `services/` | pipeline + `ConnectorETLService` base |
| `tasks.py`, `scheduled_tasks/` | Celery execution and APScheduler registration |

Cross-source concerns are deliberately central. Record linkage cannot be answered by a
single connector, and if two connectors normalised national IDs differently, linkage
would silently stop matching and create duplicate people.

## Writing a connector

Subclass `BaseHttpSource` and `BaseMappingAdapter`, declare a `DEFAULT_CONFIG`, and call
`register_etl_source(...)` from `AppConfig.ready()` with a **lazy** `config_provider`.
`urls.py` is required even when empty — openIMIS mounts `<module>.urls` for every
installed module, and without it the whole backend fails to start.

Any column a connector emits must also be declared in `individual_schema`, or
`validate_dataframe_headers` rejects the entire upload. Both that schema and a
connector's `field_map` are **runtime configuration**, set through the Django admin per
deployment rather than in migrations - so after configuring either, check they agree:

```bash
manage.py etl_check_schema              # all connectors
manage.py etl_check_schema --print-missing
```

A connector should be thin. `zm_etl_kobo`'s adapter contains no logic at all.

## Configuration

Each connector owns its own `ModuleConfiguration` row. Precedence, deep-merged:

```
SOURCE_DEFAULTS  <-  connector DEFAULT_CONFIG  <-  stored ModuleConfiguration
```

Credentials are referenced as `"env:VAR_NAME"` and read from the process environment —
never stored, because that table appears in every database backup.

## Running the tests

```bash
manage.py test api_etl --nomigrations
```

**`--nomigrations` is required.** Without it the test database cannot be created at all:
`core.0001_initial` fails with `relation "tblUsers" does not exist`, being a legacy
migration that assumes the pre-existing openIMIS schema. A development database works
only because it is built from the openIMIS DB image, which ships those tables.

Two consequences worth knowing: the schema is built from models, so data migrations
never run and reference tables are empty (a test needing a user must seed `tblLanguages`
first — see `tests/test_individual_import_sink.py`); and a leftover `test_imis` from an
interrupted run blocks the next one with a prompt that fails as `EOFError` on a non-tty,
so drop that database and re-run.

Covered: configuration deep-merge, the registry, the mapping adapter (national-ID
normalisation, household/role emission), both paginators, auth fail-closed behaviour,
and the connector/schema contract. **Not** covered: the sink's record-linkage branches
and household formation — currently the highest-value gap.

## Operating

```bash
manage.py etl_reset_cursor --list     # sync state for every source
manage.py etl_reset_cursor <source>   # clear a cursor
```

Runs are triggered from *Import Data - API* in the frontend, by APScheduler, or by
enqueuing `api_etl.run_etl_source`. All three take the same path through a Celery worker.

## Differences from upstream

- explicit connector registry instead of `api_etl.services` package introspection, which
  cannot see classes defined in another Django module
- per-source configuration (upstream is one URL / one credential set per process)
- `ETLSyncState`: resumable incremental sync with a concurrency lease
- cross-source record linkage on national ID, with a secondary-field guard
- migrations granting `953001`/`953002` — upstream ships none, so the module is
  unusable out of the box
- household grouping actually reachable (`group_code` emitted and passed through)
- `_error_result` returns `message` rather than `"D"`, which the caller reads

Upstream remains the `upstream` remote; changes here are intended to be rebasable, and
the registry is worth offering back.
