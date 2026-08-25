"""Schema columns and index needed by cross-source record linkage.

Adds the linkage bookkeeping columns to `individual_schema` (they are emitted by the
sink, and `validate_dataframe_headers` rejects any undeclared column), plus a functional
index so matching a batch of national ids does not table-scan.
"""
import json

from django.db import connection, migrations

INDIVIDUAL_MODULE = 'individual'
LAYER = 'be'

LINKAGE_COLUMNS = {
    # Both source identifiers on a linked individual, so later syncs from either side
    # match on external_id and never re-enter linkage.
    'alt_external_ids': {'type': 'string'},
    # Set when a national_id matched but the link was NOT made automatically.
    'linkage_candidate_id': {'type': 'string'},
    'linkage_note': {'type': 'string'},
}

INDEX_NAME = 'idx_individual_json_national_id'
CREATE_INDEX = f'''
    CREATE INDEX IF NOT EXISTS {INDEX_NAME}
    ON individual_individual (("Json_ext" ->> 'national_id'))
    WHERE "isDeleted" = false;
'''
DROP_INDEX = f'DROP INDEX IF EXISTS {INDEX_NAME};'


def _row(apps):
    ModuleConfiguration = apps.get_model('core', 'ModuleConfiguration')
    return ModuleConfiguration, ModuleConfiguration.objects.filter(
        module=INDIVIDUAL_MODULE, layer=LAYER).first()


def _schema(row):
    if row and row.config:
        try:
            cfg = json.loads(row.config)
        except ValueError:
            cfg = {}
        raw = cfg.get('individual_schema')
        if raw:
            return cfg, (json.loads(raw) if isinstance(raw, str) else raw)
        return cfg, _default_schema()
    return {}, _default_schema()


def _default_schema():
    from individual.apps import DEFAULT_CONFIG
    raw = DEFAULT_CONFIG.get('individual_schema')
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def add_columns(apps, schema_editor):
    ModuleConfiguration, row = _row(apps)
    cfg, schema = _schema(row)
    properties = schema.setdefault('properties', {})
    for name, spec in LINKAGE_COLUMNS.items():
        properties.setdefault(name, spec)
    cfg['individual_schema'] = json.dumps(schema)

    if row:
        row.config = json.dumps(cfg)
        row.save()
    else:
        ModuleConfiguration.objects.create(
            module=INDIVIDUAL_MODULE, layer=LAYER, version='1',
            config=json.dumps(cfg), is_exposed=False)


def remove_columns(apps, schema_editor):
    ModuleConfiguration, row = _row(apps)
    if not row:
        return
    try:
        cfg = json.loads(row.config) if row.config else {}
    except ValueError:
        return
    raw = cfg.get('individual_schema')
    schema = json.loads(raw) if isinstance(raw, str) else (raw or {})
    properties = schema.get('properties', {})
    for name, spec in LINKAGE_COLUMNS.items():
        if properties.get(name) == spec:
            properties.pop(name, None)
    cfg['individual_schema'] = json.dumps(schema)
    row.config = json.dumps(cfg)
    row.save()


def create_index(apps, schema_editor):
    # Functional index on a JSON path - PostgreSQL only. MSSQL deployments simply run
    # without it; correctness is unaffected, only speed.
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute(CREATE_INDEX)


def drop_index(apps, schema_editor):
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute(DROP_INDEX)


class Migration(migrations.Migration):

    dependencies = [
        ('api_etl', '0003_etl_sync_state'),
        ('core', '0002_auto_20190726_0701'),
    ]

    operations = [
        migrations.RunPython(add_columns, remove_columns),
        migrations.RunPython(create_index, drop_index),
    ]
