from django.db import migrations

# 953001 - query/enumerate the registered ETL services
# 953002 - execute an ETL service
# Both are declared in api_etl/apps.py DEFAULT_CONFIG but upstream ships no migration
# granting them, so every user gets PermissionError from etlServicesByServiceName and
# the module is unusable out of the box.
api_etl_rights = [953001, 953002]
imis_administrator_system = 64


def add_rights(apps, schema_editor):
    role_model = apps.get_model('core', 'role')
    role = role_model.objects.filter(is_system=imis_administrator_system).first()
    if role is None:
        # No system administrator role in this database - nothing to grant to.
        return
    for right_id in api_etl_rights:
        if not apps.get_model('core', 'roleright').objects.filter(validity_to__isnull=True, role=role,
                                                                  right_id=right_id).exists():
            _add_right_for_role(apps, role, right_id)


def _add_right_for_role(apps, role, right_id):
    apps.get_model('core', 'roleright').objects.create(role=role, right_id=right_id, audit_user_id=1)


def remove_rights(apps, schema_editor):
    apps.get_model('core', 'roleright').objects.filter(
        role__is_system=imis_administrator_system,
        right_id__in=api_etl_rights,
        validity_to__isnull=True
    ).delete()


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        # Role and RoleRight are created by core.0008, and RoleRight.validity_from only gets
        # its default in core.0020. A shallower dependency lets the migration run first on a
        # fresh database (LookupError: App 'core' doesn't have a 'role' model).
        ('core', '0020_add_missing_fields_to_django_scheme'),
    ]

    operations = [
        migrations.RunPython(add_rights, remove_rights),
    ]
