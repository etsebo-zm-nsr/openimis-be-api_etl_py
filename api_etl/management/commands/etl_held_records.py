"""List records an ETL source held back because a required value was missing.

Held records are persisted in `ETLHeldRecord` (see its docstring); this prints, per source,
how many are open and how many were held for each missing field.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from api_etl.models import ETLHeldRecord


class Command(BaseCommand):
    help = "Show records held back by ETL sources, with counts per reason."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true",
                            help="Include resolved records (default: open only)")
        parser.add_argument("--source", default=None, help="Only this source, e.g. zispis")

    def handle(self, *args, **options):
        qs = ETLHeldRecord.objects.all()
        if not options["all"]:
            qs = qs.filter(resolved_at__isnull=True)
        if options["source"]:
            qs = qs.filter(source_name=options["source"])

        per_source = {}
        for source, reasons in qs.order_by("source_name").values_list("source_name", "reasons"):
            entry = per_source.setdefault(source, {"count": 0, "reasons": Counter()})
            entry["count"] += 1
            entry["reasons"].update(reasons)

        if not per_source:
            self.stdout.write("No held records.")
            return
        label = "held (incl. resolved)" if options["all"] else "open"
        for source, entry in per_source.items():
            self.stdout.write(f"{source}: {entry['count']} {label}")
            for reason, n in sorted(entry["reasons"].items()):
                self.stdout.write(f"  {reason}: {n}")
