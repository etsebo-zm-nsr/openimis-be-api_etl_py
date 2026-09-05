"""Restore household roles and primary recipients lost during upstream grouping.

Run after approving a household import task. See api_etl/household_roles.py for what
goes wrong and why it cannot be prevented from the emitting side.

    manage.py etl_repair_household_roles --dry-run
    manage.py etl_repair_household_roles
    manage.py etl_repair_household_roles --code zm-hh-zispis:100200/68/1
"""
from django.core.management.base import BaseCommand

from api_etl.household_roles import reconcile_household_roles


class Command(BaseCommand):
    help = "Repair household roles and primary recipients after an ETL group import."

    def add_arguments(self, parser):
        parser.add_argument("--code", action="append", dest="codes",
                            help="limit to this group code (repeatable)")
        parser.add_argument("--dry-run", action="store_true",
                            help="report what would change without writing")

    def handle(self, *args, **options):
        summary = reconcile_household_roles(
            group_codes=options.get("codes"), dry_run=options["dry_run"])
        style = self.style.WARNING if (
            summary["roles_restored"] or summary["recipients_moved"]) else self.style.SUCCESS
        self.stdout.write(style(
            f"households examined      : {summary['groups']}\n"
            f"roles restored           : {summary['roles_restored']}\n"
            f"primary recipients moved : {summary['recipients_moved']}\n"
            f"households skipped       : {summary['no_source']} (no ETL household_role)"
        ))
        if options["dry_run"]:
            self.stdout.write("(dry run - nothing was written)")
