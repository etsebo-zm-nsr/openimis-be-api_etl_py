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

    def parse_date(self, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        if isinstance(value, (datetime, date)):
            return value.strftime("%Y-%m-%d")
        text = str(value).strip()
        for fmt in self.cfg.adapter.date_formats:
            try:
                return datetime.strptime(text[:len(datetime.now().strftime(fmt)) + 4], fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        # ISO-8601 with a time component is the common case the formats list misses.
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            logger.debug("api_etl[%s]: unparseable date %r", self.cfg.name, value)
            return None

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
            if record is not None:
                out.append(record)
        return out

    def transform_row(self, row: Any) -> Optional[dict]:
        adapter = self.cfg.adapter
        record: dict = {}

        for target, path in (adapter.field_map or {}).items():
            record[target] = self.resolve_path(row, path)
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
