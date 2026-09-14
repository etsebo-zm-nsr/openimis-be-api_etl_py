"""Load a location hierarchy from a gazetteer CSV into tblLocations.

    manage.py etl_load_locations GEO_FILE.csv --dry-run
    manage.py etl_load_locations GEO_FILE.csv

Nothing imports until this tree exists: openIMIS resolves a person's location by joining
tblLocations on name AND code with the leaf type, so an empty tree rejects every row.

`Location.code` is the DOTTED PATH of codes from the top (`9.903.130.9`). Gazetteer codes
are normally parent-scoped rather than national - Zambia's file has 180 distinct
`ward_code` values for ~1,770 wards - so a bare code identifies nothing and the path is
what makes a node unique.

--levels maps CSV columns onto openIMIS's four tiers, as `TYPE:code_column:name_column`,
outermost first. The default is Zambia's GEO file; any other gazetteer is a flag change.

Idempotent: a node already present with the same path keeps its identity, so re-running
after a corrected export updates names rather than duplicating the tree.
"""
import csv
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api_etl.locations import normalise_place

DEFAULT_LEVELS = "R:province_code:province,D:district_code:district," \
                 "W:constituency_code:constituency,V:ward_code:ward"


class Command(BaseCommand):
    help = "Load a location hierarchy from a gazetteer CSV into tblLocations."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--levels", default=DEFAULT_LEVELS,
                            help="TYPE:code_column:name_column, outermost first")
        parser.add_argument("--dry-run", action="store_true",
                            help="report what would change, and every defect found")
        parser.add_argument("--encoding", default="utf-8-sig")

    def handle(self, *args, **options):
        levels = []
        for part in options["levels"].split(","):
            bits = part.split(":")
            if len(bits) != 3:
                raise CommandError(f"--levels entry {part!r} is not TYPE:code_col:name_col")
            levels.append(tuple(bits))

        try:
            with open(options["csv_path"], encoding=options["encoding"]) as handle:
                rows = list(csv.DictReader(handle))
        except OSError as exc:
            raise CommandError(f"cannot read {options['csv_path']}: {exc}")
        if not rows:
            raise CommandError("the file has no rows")

        missing = {col for _, code_col, name_col in levels for col in (code_col, name_col)
                   if col not in rows[0]}
        if missing:
            raise CommandError(f"columns not in the file: {sorted(missing)}")

        self.stdout.write(f"{len(rows)} row(s), {len(levels)} level(s)\n")
        defects = self._check(rows, levels)
        if defects and options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                "\nThese must be corrected in the source file - a name duplicated inside "
                "one parent cannot be resolved from a source that sends names only."))
        if options["dry_run"]:
            self._plan(rows, levels)
            self.stdout.write("\n(dry run - nothing written)")
            return
        if defects:
            raise CommandError(
                f"{defects} defect(s) found; re-run with --dry-run for the detail, or "
                f"correct the file first. Loading a tree with duplicated names inside a "
                f"parent would make those locations unresolvable.")
        self._load(rows, levels)

    # ------------------------------------------------------------------ checks

    def _check(self, rows, levels):
        """Uniqueness is only required WITHIN a parent - that is how gazetteer codes are
        scoped. Report both directions: one code with two names, and one name with two
        codes. The second is what breaks name-based resolution."""
        total = 0
        for depth, (_, code_col, name_col) in enumerate(levels):
            parents = [levels[i][1] for i in range(depth)]
            by_code, by_name = defaultdict(set), defaultdict(set)
            for row in rows:
                key = tuple((row.get(p) or "").strip() for p in parents)
                by_code[key + ((row.get(code_col) or "").strip(),)].add(
                    normalise_place(row.get(name_col)))
                by_name[key + (normalise_place(row.get(name_col)),)].add(
                    (row.get(code_col) or "").strip())
            code_clash = {k: v for k, v in by_code.items() if len(v) > 1}
            name_clash = {k: v for k, v in by_name.items() if len(v) > 1}
            total += len(code_clash) + len(name_clash)
            label = name_col
            if code_clash or name_clash:
                self.stdout.write(self.style.ERROR(
                    f"  {label}: {len(code_clash)} code(s) with several names, "
                    f"{len(name_clash)} name(s) with several codes"))
                for k, v in list(code_clash.items())[:5]:
                    self.stdout.write(f"      code {k[-1]!r} under {'/'.join(k[:-1]) or 'top'}"
                                      f" -> {sorted(x for x in v if x)}")
                for k, v in list(name_clash.items())[:5]:
                    self.stdout.write(f"      name {k[-1]!r} under {'/'.join(k[:-1]) or 'top'}"
                                      f" -> codes {sorted(v)}")
            else:
                self.stdout.write(self.style.SUCCESS(f"  {label}: clean"))
        return total

    def _plan(self, rows, levels):
        from location.models import Location
        seen = defaultdict(set)
        for row in rows:
            codes = []
            for _, code_col, _ in levels:
                codes.append((row.get(code_col) or "").strip())
                seen[len(codes)].add(".".join(codes))
        self.stdout.write("\nwould ensure:")
        for depth, (type_, _, name_col) in enumerate(levels, start=1):
            existing = Location.objects.filter(type=type_, validity_to__isnull=True).count()
            self.stdout.write(f"  {type_} ({name_col:13}): {len(seen[depth]):5} node(s)"
                              f"   [{existing} currently in the database]")

    # ------------------------------------------------------------------- load

    @transaction.atomic
    def _load(self, rows, levels):
        from location.models import Location

        created = defaultdict(int)
        renamed = defaultdict(int)
        cache = {}
        for row in rows:
            parent, codes = None, []
            for type_, code_col, name_col in levels:
                code = (row.get(code_col) or "").strip()
                name = (row.get(name_col) or "").strip()
                if not code or not name:
                    parent = None
                    break
                codes.append(code)
                path = ".".join(codes)
                if len(path) > 50:
                    raise CommandError(
                        f"code path {path!r} exceeds the 50-character column limit")
                node = cache.get((type_, path))
                if node is None:
                    node = Location.objects.filter(
                        code=path, type=type_, validity_to__isnull=True).first()
                    if node is None:
                        node = Location(code=path, type=type_, name=name[:50], parent=parent)
                        node.save()
                        created[type_] += 1
                    elif node.name != name[:50]:
                        node.name = name[:50]
                        node.save()
                        renamed[type_] += 1
                    cache[(type_, path)] = node
                parent = node

        self.stdout.write(self.style.SUCCESS("\nloaded:"))
        for type_, _, name_col in levels:
            self.stdout.write(f"  {type_} ({name_col:13}): {created[type_]:5} created, "
                              f"{renamed[type_]:5} renamed")
