"""Declare the ETL-emitted columns on the `individual` module's JSON schema.

`individual/workflows/utils.py::validate_dataframe_headers` asserts

    (df_headers - required_headers).issubset(schema_properties)

so ANY column an ETL adapter emits must be declared in `individual_schema.properties`,
except the base fields and the three magic ones (recipient_info, group_code,
individual_role). Upstream's own ExampleIndividualAdapter emits `external_id`, which is
undeclared - so the shipped pipeline fails header validation as-is.

This migration merges the shared ETL column set into the effective schema. It is an
idempotent MERGE, never a wholesale replace: connectors that need extra columns ship
their own migration doing the same, and the two compose because migrations run
sequentially. `ModuleConfiguration.get_or_default` shallow-merges
`{**defaults, **db_config}`, so we persist only the keys we actually override and
everything else keeps tracking upstream defaults.
"""
import json

from django.db import migrations

INDIVIDUAL_MODULE = 'individual'
LAYER = 'be'

# Columns emitted by BaseMappingAdapter for every connector.
# national_id / national_id_type / beneficiary_data_source are already in the upstream
# default schema; they are listed here so the set is explicit and so this still works
# against a database whose schema was trimmed.
ETL_COLUMNS = {
    'external_id': {'type': 'string'},
    'national_id': {'type': 'string'},
    'national_id_type': {'type': 'string'},
    'beneficiary_data_source': {'type': 'string'},
    'source_batch_id': {'type': 'string'},
    # group_code is stripped from json_ext by _clean_json_ext() after grouping, so keep
    # an untouched copy for traceability/debugging.
    'household_ref': {'type': 'string'},
}

# beneficiary_data_source is provenance, not PII, but ships inside individual_mask_fields
# with masking enabled by default - which renders it masked in the UI.
UNMASK_FIELDS = ['json_ext.beneficiary_data_source']


def _get_row(apps):
    ModuleConfiguration = apps.get_model('core', 'ModuleConfiguration')
    return ModuleConfiguration, ModuleConfiguration.objects.filter(
        module=INDIVIDUAL_MODULE, layer=LAYER
    ).first()


def _effective(row, key):
    """Value currently in force: the DB override if present, else the app default."""
    if row and row.config:
        try:
            cfg = json.loads(row.config)
        except ValueError:
            cfg = {}
        if key in cfg:
            return cfg[key]
    from individual.apps import DEFAULT_CONFIG
    return DEFAULT_CONFIG.get(key)


def _load_schema(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    return raw or {}


def add_etl_columns(apps, schema_editor):
    ModuleConfiguration, row = _get_row(apps)

    schema = _load_schema(_effective(row, 'individual_schema'))
    properties = schema.setdefault('properties', {})
    for name, spec in ETL_COLUMNS.items():
        properties.setdefault(name, spec)

    mask_fields = list(_effective(row, 'individual_mask_fields') or [])
    mask_fields = [f for f in mask_fields if f not in UNMASK_FIELDS]

    overrides = {
        'individual_schema': json.dumps(schema),
        'individual_mask_fields': mask_fields,
    }

    if row:
        try:
            cfg = json.loads(row.config) if row.config else {}
        except ValueError:
            cfg = {}
        cfg.update(overrides)
        row.config = json.dumps(cfg)
        row.save()
    else:
        ModuleConfiguration.objects.create(
            module=INDIVIDUAL_MODULE,
            layer=LAYER,
            version='1',
            config=json.dumps(overrides),
            is_exposed=False,
        )


def remove_etl_columns(apps, schema_editor):
    ModuleConfiguration, row = _get_row(apps)
    if not row:
        return
    try:
        cfg = json.loads(row.config) if row.config else {}
    except ValueError:
        return

    schema = _load_schema(cfg.get('individual_schema'))
    properties = schema.get('properties', {})
    for name in ETL_COLUMNS:
        # Only drop the ones we added, and only if untouched since.
        if properties.get(name) == ETL_COLUMNS[name]:
            properties.pop(name, None)
    cfg['individual_schema'] = json.dumps(schema)

    mask_fields = list(cfg.get('individual_mask_fields') or [])
    for field in UNMASK_FIELDS:
        if field not in mask_fields:
            mask_fields.append(field)
    cfg['individual_mask_fields'] = mask_fields

    # If this migration was what created the row, take it away again.
    if set(cfg.keys()) <= {'individual_schema', 'individual_mask_fields'}:
        row.delete()
    else:
        row.config = json.dumps(cfg)
        row.save()


class Migration(migrations.Migration):

    dependencies = [
        ('api_etl', '0001_add_api_etl_rights_to_admin'),
        ('individual', '__first__'),
        # NOT core.__first__: `layer` (and is_exposed) are added to ModuleConfiguration by
        # core.0002, and apps.get_model() in a migration yields the HISTORICAL model as of
        # this node's dependencies. Depending on __first__ gives a ModuleConfiguration with
        # only (id, module, version, config), so filtering on `layer` raises
        # FieldError: Cannot resolve keyword 'layer' into field.
        ('core', '0002_auto_20190726_0701'),
    ]

    operations = [
        migrations.RunPython(add_etl_columns, remove_etl_columns),
    ]
