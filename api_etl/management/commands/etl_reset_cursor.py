"""Reset a source's incremental sync cursor.

Operational escape hatch: forces the next run to re-pull from the beginning (or from a
given point) after a bad mapping, a source-side data correction, or a stuck lease.
"""
from django.core.management.base import BaseCommand, CommandError

from api_etl.models import ETLSyncState
from api_etl.registry import registered_names


class Command(BaseCommand):
    help = "Reset the ETL sync cursor for a source (use --list to see current state)."

    def add_arguments(self, parser):
        parser.add_argument("source", nargs="?", help="Registered source name, e.g. zispis")
        parser.add_argument("--cursor", default=None,
                            help="Seed the cursor with this value instead of clearing it")
        parser.add_argument("--list", action="store_true", help="Show sync state and exit")

    def handle(self, *args, **options):
        if options["list"]:
            registered = registered_names(include_disabled=True)
            for state in ETLSyncState.objects.order_by("source_name"):
                mark = "" if state.source_name in registered else "  (not registered)"
                self.stdout.write(
                    f"{state.source_name:20} {state.status:8} cursor={state.cursor!r} "
                    f"pulled={state.records_pulled} batches={state.batches_pushed}{mark}"
                )
            missing = [n for n in registered
                       if not ETLSyncState.objects.filter(source_name=n).exists()]
            for name in missing:
                self.stdout.write(f"{name:20} {'-':8} (never run)")
            return

        source = options["source"]
        if not source:
            raise CommandError("Provide a source name, or --list")
        if source not in registered_names(include_disabled=True):
            raise CommandError(
                f"Unknown ETL source {source!r}. Registered: {registered_names(include_disabled=True)}"
            )

        state = ETLSyncState.reset(source, cursor=options["cursor"])
        self.stdout.write(self.style.SUCCESS(
            f"Reset {source}: status={state.status} cursor={state.cursor!r}"
        ))
