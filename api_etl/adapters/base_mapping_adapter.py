"""Config-driven adapter shared by all connectors.

A connector normally subclasses this and supplies only a `field_map` in its config;
anything genuinely source-shaped can be handled by overriding `transform_row`.

Several rules are enforced here rather than left to connectors, because getting them
wrong is silent and damaging:

* `national_id` is normalised IDENTICALLY for every source. Cross-source record linkage
  matches on this value - if two connectors normalised differently, linkage would
  silently stop matching and create duplicate people.
* `group_code` is never blank. `_get_grouped_individuals()` excludes rows with a blank
  or null group code, so a blank silently drops the row from household formation.
  (Malawi's msr_etl carries a `#TODO: track skipped households` at exactly this point.)
* `individual_role` is always a valid `GroupIndividual.Role` ATTRIBUTE name.
  `_individual_role_parser` calls `.upper()` on the value, so a NaN from an empty CSV
  cell raises AttributeError.
* `external_id` and `group_code` are namespaced per source, so two sources' identifier
  spaces cannot collide.
"""
import hashlib
import logging
import re
import unicodedata
from datetime import date, datetime
from typing import Any, Iterable, Optional

from api_etl.adapters.base import DataAdapter
from api_etl.locations import LocationIndex, apply_aliases

logger = logging.getLogger(__name__)

# Columns individual/ treats specially; they must not be declared in individual_schema.
MAGIC_COLUMNS = ("recipient_info", "group_code", "individual_role")

RECIPIENT_PRIMARY = "1"
RECIPIENT_SECONDARY = "2"

# pandas.read_csv's default `na_values` (see the pandas docs for `read_csv`) - the exact,
# case-sensitive tokens that become NaN when openIMIS re-reads our CSV. A name of "n/a"
# passes an emptiness check here, then becomes NaN there and aborts the whole batch on
# the NOT NULL constraint. Checking against this set here, before the CSV round-trip,
# catches it while there is still a chance to hold the record back individually.
PANDAS_DEFAULT_NA_VALUES = frozenset({
    "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan",
    "1.#IND", "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a",
    "nan", "null",
})

# Name values that are placeholders rather than names. Flagged, never altered or dropped.
NAME_PLACEHOLDERS = frozenset({
    "UNKNOWN", "NONE", "NIL", "NULL", "TEST", "XXX", "NO NAME", "NONAME", "-", ".",
})
_NRC_PATTERN = re.compile(r"\d{6}\s*/\s*\d{2}\s*/\s*\d")


class BaseMappingAdapter(DataAdapter):

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        # (external_id, [missing field, ...]) for records held back this run.
        self.skipped: list = []
        # (external_id, [missing field, ...], raw source row) for the same records, so the
        # service can persist what was held back. `skipped` keeps its 2-tuple shape.
        self.held_rows: list = []
        # Rows excluded by `adapter.include_when` - a deliberate filter, not a fault.
        self.filtered: int = 0
        # external_ids whose place names did not match the loaded location tree.
        self.unresolved_locations: list = []
        # external_ids resolved only by the district+ward fallback (constituency ignored).
        self.fallback_locations: list = []
        match_fields = self.cfg.adapter.location_match_fields or []
        self._locations = LocationIndex(depth=len(match_fields)) if match_fields else None

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def resolve_path(row: Any, path: str) -> Any:
        """Resolve 'a/b/c' or 'a.b.c' against nested dicts.

        Kobo flattens repeat groups into keys like 'household/hoh_first_name', so the
        separator has to handle both a literal key containing '/' and real nesting.
        """
        if row is None or not path:
            return None
        if isinstance(row, dict) and path in row:      # literal key wins
            return row[path]
        current = row
        for part in path.replace(".", "/").split("/"):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current

    @staticmethod
    def normalise_national_id(value: Any) -> Optional[str]:
        """Canonical form used for cross-source linkage. Must not vary by connector."""
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        cleaned = "".join(ch for ch in text if ch.isalnum())
        return cleaned.upper() or None

    def clean_text_reporting(self, value: Any):
        """Return `(cleaned, removed)`; `removed` is True when characters were dropped.

        Whitespace is collapsed (so `\\n` and `\\t` become spaces, not removals). What is
        REMOVED is any remaining control character (category Cc) and lone surrogate
        (Cs, which PostgreSQL rejects). Text is NFC-normalised first so a decomposed
        N + combining tilde equals the precomposed letter for linkage and search. Valid
        Unicode letters are never altered beyond that.
        """
        if not isinstance(value, str) or not self.cfg.adapter.clean_whitespace:
            return value, False
        text = " ".join(unicodedata.normalize("NFC", value).split())
        kept = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cs"))
        removed = kept != text
        return (" ".join(kept.split()) or None), removed

    def clean_text(self, value: Any) -> Any:
        """Collapse whitespace and drop control characters from a string value.

        Third-party exports routinely carry leading newlines (`'\\nKaboyi'`) and doubled
        internal spaces. Left alone these are invisible in a UI and corrosive underneath:
        record linkage confirms a national-ID match on surname, and `'\\nKaboyi'` never
        equals `'Kaboyi'`. Non-strings pass through untouched.
        """
        return self.clean_text_reporting(value)[0]

    def name_problems(self, record: dict) -> list:
        """Quality problem codes for the configured name fields. Values are not changed."""
        fields = list(self.cfg.adapter.name_fields or [])
        codes = []
        for name in fields:
            value = record.get(name)
            if not isinstance(value, str) or not value.strip():
                continue
            text = value.strip()
            if any(ch.isdigit() for ch in text):
                codes.append("contains_digits")
            if _NRC_PATTERN.search(text):
                codes.append("looks_like_nrc")
            if text.upper() in NAME_PLACEHOLDERS:
                codes.append("placeholder")
            if len(text) == 1:
                codes.append("single_character")
        if "first_name" in fields and "last_name" in fields:
            first, last = record.get("first_name"), record.get("last_name")
            if (isinstance(first, str) and isinstance(last, str) and first.strip()
                    and first.strip().casefold() == last.strip().casefold()):
                codes.append("first_equals_last")
        return sorted(set(codes))

    def _plausible(self, parsed: datetime) -> bool:
        """Reject dates that are data-entry artefacts rather than dates.

        A birth year of 0002 or 1091 is not a fact the registry should assert. Storing
        None is honest; storing the artefact both corrupts the record and silently
        weakens linkage, which uses date of birth as one of its two confirmations.
        """
        adapter = self.cfg.adapter
        if adapter.min_year and parsed.year < adapter.min_year:
            return False
        max_year = adapter.max_year or datetime.now().year
        return parsed.year <= max_year

    def parse_date(self, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        parsed = None
        if isinstance(value, (datetime, date)):
            parsed = datetime(value.year, value.month, value.day)
        else:
            text = str(value).strip()
            for fmt in self.cfg.adapter.date_formats:
                try:
                    parsed = datetime.strptime(
                        text[:len(datetime.now().strftime(fmt)) + 4], fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                # ISO-8601 with a time component is the common case the formats miss.
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    logger.debug("api_etl[%s]: unparseable date %r", self.cfg.name, value)
                    return None
        if not self._plausible(parsed):
            logger.debug("api_etl[%s]: implausible date %r - storing nothing",
                         self.cfg.name, value)
            return None
        return parsed.strftime("%Y-%m-%d")

    def map_role(self, raw_role: Any) -> str:
        """Map a source role value to a GroupIndividual.Role attribute NAME."""
        adapter = self.cfg.adapter
        default = adapter.default_role or "OTHER_RELATIVE"
        if raw_role in (None, ""):
            return default
        key = str(raw_role).strip()
        mapped = adapter.role_map.get(key) or adapter.role_map.get(key.lower()) or key
        mapped = str(mapped).strip().upper().replace(" ", "_").replace("-", "_")
        return mapped if self._is_valid_role(mapped) else default

    @staticmethod
    def _is_valid_role(name: str) -> bool:
        try:
            from individual.models import GroupIndividual
        except Exception:      # pragma: no cover - individual always present in practice
            return True
        return getattr(GroupIndividual.Role, name, None) is not None

    def _namespaced(self, value: Any, prefix: str) -> Optional[str]:
        if value in (None, ""):
            return None
        return f"{prefix}{value}"

    # ---------------------------------------------------------------- transform

    def transform(self, data: Iterable[Any]) -> Iterable[Any]:
        if data is None:
            raise self.Error("Invalid input, expect input not to be None")
        out = []
        for row in data:
            record = self.transform_row(row)
            if record is None:
                continue
            missing = self.missing_required(record)
            if missing:
                self.skipped.append((record.get("external_id"), missing))
                self.held_rows.append((record.get("external_id"), missing, row))
                continue
            if not self.keep_row(record):
                self.filtered += 1
                continue
            self.resolve_location(record)
            key = self.identity_key(record)
            if key:
                record["identity_key"] = key
            out.append(record)
        if self.unresolved_locations:
            logger.warning(
                "api_etl[%s]: %s row(s) did not match the loaded location tree and will "
                "be imported WITHOUT a location, flagged. First few: %s", self.cfg.name,
                len(self.unresolved_locations),
                ", ".join(path for _, path in self.unresolved_locations[:5]))
        if self.fallback_locations:
            logger.warning(
                "api_etl[%s]: %s row(s) matched on district and ward only (the "
                "constituency disagrees with the loaded location tree). First few: %s",
                self.cfg.name, len(self.fallback_locations),
                ", ".join(path for _, path in self.fallback_locations[:5]))
        if self.skipped:
            by_field: dict = {}
            for _, fields in self.skipped:
                for name in fields:
                    by_field[name] = by_field.get(name, 0) + 1
            logger.warning(
                "api_etl[%s]: held back %s of %s record(s) missing a required value "
                "(%s). They are NOT in the registry and need a decision on how to "
                "represent an unknown value.",
                self.cfg.name, len(self.skipped), len(self.skipped) + len(out),
                ", ".join(f"{k}={v}" for k, v in sorted(by_field.items())),
            )
        return out

    @staticmethod
    def normalise_component(value: Any) -> Optional[str]:
        """Fold a value to its comparable form for the identity key.

        Case, accents, punctuation and spacing all vary between a MIS export and a field
        submission for the same person - "O'Brien" / "OBRIEN", "MWEMBE  ZHI". Two
        spellings that differ only in those respects must produce the same key, or
        linkage silently fails and the registry gains a duplicate person.
        """
        if value in (None, ""):
            return None
        text = unicodedata.normalize("NFKD", str(value)).upper()
        text = re.sub(r"[^A-Z0-9]+", " ", text)
        return " ".join(text.split()) or None

    def identity_key(self, record: dict) -> Optional[str]:
        """Stable hash of `adapter.identity_key_fields`, or None if any part is missing.

        None rather than a partial key on purpose: a key built from three of five
        components matches far too many people. A record that cannot supply the whole
        key simply does not participate in identity matching, and imports as new.

        Hashed rather than stored as the concatenation because this column is queried in
        bulk and would otherwise duplicate name and birth-year data into a second
        readable place.
        """
        fields = self.cfg.adapter.identity_key_fields or []
        if not fields:
            return None
        parts = []
        for name in fields:
            component = self.normalise_component(record.get(name))
            if component is None:
                return None
            parts.append(component)
        joined = "|".join(parts)
        return hashlib.blake2s(joined.encode("utf-8"), digest_size=16).hexdigest()

    def keep_row(self, record: dict) -> bool:
        """Apply `adapter.include_when`. Empty config keeps everything."""
        rules = self.cfg.adapter.include_when or {}
        for column, allowed in rules.items():
            if record.get(column) not in list(allowed or []):
                return False
        return True

    def resolve_location(self, record: dict) -> None:
        """Replace the mapped place names with the loaded tree's code and canonical name.

        openIMIS matches a person's location on name AND code together, so both have to
        come from the same place - the tree that was loaded - rather than one from the
        source and one derived. A source that sends names only therefore cannot supply
        `location_code` itself; it is looked up here.

        Resolution tries the full path first. If that fails and there are at least two
        levels, it retries on the first (district) and last (ward) level only, ignoring
        the middle: the gazetteer is authoritative, and a source's constituency is the
        level that most often disagrees. That resolves only if the ward is unique within
        the district. Every record gets `location_match` ("exact", "district_ward" or
        "unresolved"); the last two also get `source_location`, the place names as sent
        ("district / constituency / ward", "?" for a missing level), for review.

        The sink may later rewrite "unresolved" to "unresolved_kept_existing" for an
        already-imported individual whose current location it preserves.

        A row that resolves by neither route is imported WITHOUT a location, flagged
        "unresolved". This supersedes the earlier "a person placed in the wrong village
        is worse than a person rejected": the person now has NO location rather than a
        wrong one, and is never held back. `individual`'s import validation accepts a
        row whose `location_name` and `location_code` are both empty, but rejects one
        with a name and no code - so BOTH are blanked, not just the code.

        An unresolved row still gets a `location_code` key (None). Without it, a
        batch where EVERY row fails to resolve produces a CSV with no location_code
        column at all - `union_of_keys` only emits columns some row actually carries -
        and openIMIS rejects the whole upload with "missing essential header:
        location_code" instead of accepting the rows.
        """
        adapter = self.cfg.adapter
        if not self._locations:
            return
        levels = adapter.location_match_fields
        sent = " / ".join(str(self.clean_text(record.get(level)) or "?") for level in levels)
        names = apply_aliases([record.get(level) for level in levels],
                              adapter.location_aliases, levels)
        external_id = record.get("external_id")
        resolved = self._locations.resolve(names)
        if resolved is not None:
            record["location_match"] = "exact"
        else:
            resolved = self._locations.resolve_skipping_middle(names) \
                if len(levels) >= 2 else None
            if resolved is not None:
                record["location_match"] = "district_ward"
                record["source_location"] = sent
                self.fallback_locations.append((external_id, sent))
        if resolved is None:
            self.unresolved_locations.append(
                (external_id, "/".join(str(n or "?") for n in names)))
            record["location_match"] = "unresolved"
            record["source_location"] = sent
            record["location_code"] = None
            record["location_name"] = None
            return
        code, canonical = resolved
        record["location_code"] = code
        record["location_name"] = canonical

    def missing_required(self, record: dict) -> list:
        """Required columns this record cannot supply.

        Enforced before the sink rather than inside it because the import is one
        `INSERT ... SELECT`: a single row with a null `dob` aborts the whole statement,
        so 7 incomplete records out of 200 reject the other 193. Holding them back is
        not a silent drop - they are counted, logged and reported on the run.

        A non-empty value is not enough: `data_to_file` writes a plain CSV, and
        `IndividualImportService.import_loaders` reads it back with
        `pd.read_csv(f, dtype={"location_code": str})` - every OTHER column is subject
        to pandas' default NA-token detection. A first_name of "n/a" passes the naive
        emptiness check, survives to the CSV, and comes back as NaN - the same NOT NULL
        abort as a value that was missing here. So a value is treated as missing exactly
        when pandas would later read it as NaN.
        """
        missing = []
        for name in (self.cfg.adapter.required_fields or []):
            value = record.get(name)
            text = "" if value is None else str(value).strip()
            if text in PANDAS_DEFAULT_NA_VALUES:
                missing.append(name)
        return missing

    def transform_row(self, row: Any) -> Optional[dict]:
        adapter = self.cfg.adapter
        record: dict = {}

        stripped = []
        for target, path in (adapter.field_map or {}).items():
            record[target], removed = self.clean_text_reporting(self.resolve_path(row, path))
            if removed:
                stripped.append(target)
        for target, value in (adapter.constants or {}).items():
            record[target] = value

        external_id = self._namespaced(
            self.resolve_path(row, adapter.external_id_field), adapter.external_id_prefix
        )
        if not external_id:
            logger.warning("api_etl[%s]: row has no %s - skipping",
                           self.cfg.name, adapter.external_id_field)
            return None
        record["external_id"] = external_id

        # Flag, never block: values are stored as the source sent them (minus unstorable
        # characters) and the problem is recorded in json_ext for users to filter on.
        if stripped:
            record["text_quality"] = "invalid_characters_removed:" + ",".join(sorted(stripped))
        problems = self.name_problems(record)
        if problems:
            record["name_quality"] = ";".join(problems)

        if "dob" in record:
            record["dob"] = self.parse_date(record.get("dob"))

        if adapter.national_id_field:
            record["national_id"] = self.normalise_national_id(
                self.resolve_path(row, adapter.national_id_field)
            )
        elif "national_id" in record:
            record["national_id"] = self.normalise_national_id(record["national_id"])
        if adapter.national_id_type_field:
            record["national_id_type"] = self.resolve_path(row, adapter.national_id_type_field)

        if adapter.group_code_field:
            group_code = self._namespaced(
                self.resolve_path(row, adapter.group_code_field), adapter.group_code_prefix
            )
            if group_code:
                # group_code is consumed and stripped by _clean_json_ext() after grouping;
                # household_ref keeps an untouched copy for traceability.
                record["group_code"] = group_code
                record["household_ref"] = group_code
                role = self.map_role(
                    self.resolve_path(row, adapter.role_field) if adapter.role_field else None
                )
                record["individual_role"] = role
                # A durable copy. `individual_role` is stripped from the individual by
                # `_clean_json_ext()` once grouping is done, and grouping is exactly
                # where upstream loses roles - so the value has to outlive it for
                # `reconcile_household_roles` to have anything to restore from.
                record["household_role"] = role
                if adapter.recipient_field:
                    record["recipient_info"] = self.map_recipient(
                        self.resolve_path(row, adapter.recipient_field)
                    )
                    # Durable copy: upstream strips `recipient_info` after grouping, and
                    # a later post-import step needs to know the source declared it.
                    record["household_recipient"] = record["recipient_info"]
            else:
                logger.warning(
                    "api_etl[%s]: row %s has no group code - it will import as an individual "
                    "with no household", self.cfg.name, external_id
                )

        if self.cfg.provenance.data_source_label:
            record["beneficiary_data_source"] = self.cfg.provenance.data_source_label

        return record

    @staticmethod
    def map_recipient(value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip().lower()
        if text in ("1", "primary", "true", "yes", "head"):
            return RECIPIENT_PRIMARY
        if text in ("2", "secondary"):
            return RECIPIENT_SECONDARY
        return None
