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
import logging
from datetime import date, datetime
from typing import Any, Iterable, Optional

from api_etl.adapters.base import DataAdapter

logger = logging.getLogger(__name__)

# Columns individual/ treats specially; they must not be declared in individual_schema.
MAGIC_COLUMNS = ("recipient_info", "group_code", "individual_role")

RECIPIENT_PRIMARY = "1"
RECIPIENT_SECONDARY = "2"


class BaseMappingAdapter(DataAdapter):

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        # (external_id, [missing field, ...]) for records held back this run.
        self.skipped: list = []

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

    def clean_text(self, value: Any) -> Any:
        """Collapse whitespace and drop control characters from a string value.

        Third-party exports routinely carry leading newlines (`'\\nKaboyi'`) and doubled
        internal spaces. Left alone these are invisible in a UI and corrosive underneath:
        record linkage confirms a national-ID match on surname, and `'\\nKaboyi'` never
        equals `'Kaboyi'`. Non-strings pass through untouched.
        """
        if not isinstance(value, str) or not self.cfg.adapter.clean_whitespace:
            return value
        return " ".join(value.split()) or None

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
                continue
            out.append(record)
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

    def missing_required(self, record: dict) -> list:
        """Required columns this record cannot supply.

        Enforced before the sink rather than inside it because the import is one
        `INSERT ... SELECT`: a single row with a null `dob` aborts the whole statement,
        so 7 incomplete records out of 200 reject the other 193. Holding them back is
        not a silent drop - they are counted, logged and reported on the run.
        """
        return [name for name in (self.cfg.adapter.required_fields or [])
                if record.get(name) in (None, "")]

    def transform_row(self, row: Any) -> Optional[dict]:
        adapter = self.cfg.adapter
        record: dict = {}

        for target, path in (adapter.field_map or {}).items():
            record[target] = self.clean_text(self.resolve_path(row, path))
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
                record["individual_role"] = self.map_role(
                    self.resolve_path(row, adapter.role_field) if adapter.role_field else None
                )
                if adapter.recipient_field:
                    record["recipient_info"] = self.map_recipient(
                        self.resolve_path(row, adapter.recipient_field)
                    )
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
