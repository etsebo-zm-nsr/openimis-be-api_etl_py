"""Loading a location hierarchy, and resolving source names against it.

Two halves of one problem. A gazetteer supplies the authoritative tree; an external
source sends place NAMES and expects them to land on the right node.

Codes in a gazetteer are normally **parent-scoped** - unique among siblings, not
nationally. Zambia's file has 180 distinct `ward_code` values for ~1,770 wards. So a
location's identifier is the dotted path of codes from the top, `9.903.130.9`, and that
is what goes into `Location.code`.

Resolution deliberately ignores the source's TOP level. A district name implies its
province (measured: 116 district names, none spanning two provinces), so matching on
district downwards gives exactly the same answer as the full path while being immune to
a source holding a stale province after a boundary change - which accounts for the
largest group of mismatches in practice.
"""
import logging
import re
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def normalise_place(value) -> Optional[str]:
    """Fold a place name to its comparable form.

    Gazetteer and source disagree on case, punctuation and spacing for the same place -
    `SINJEMBELA(SHANGOMBO)` vs `SINJEMBELA (SHANGOMBO)`, `CHAMA NORTH` vs `CHAMA-NORTH`.
    Without this they are different strings and the row is rejected.
    """
    if value in (None, ""):
        return None
    text = unicodedata.normalize("NFKD", str(value)).upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", text).split()) or None


class LocationIndex:
    """Lookup from a tuple of place names to a loaded location's code.

    Built once per run: one query, then resolution is in memory. The index is keyed on
    the LAST `depth` names of each leaf's ancestry, so a caller matching
    (district, constituency, ward) and one matching the full path share the same index.
    """

    def __init__(self, leaf_type: str = "V", depth: int = 3):
        self.leaf_type = leaf_type
        self.depth = depth
        self._index: Optional[Dict[Tuple, Tuple[str, str]]] = None
        self.ambiguous: Dict[Tuple, int] = {}

    def _build(self) -> Dict[Tuple, Tuple[str, str]]:
        from location.models import Location

        rows = (Location.objects.filter(type=self.leaf_type, validity_to__isnull=True)
                .select_related("parent", "parent__parent", "parent__parent__parent"))
        index: Dict[Tuple, Tuple[str, str]] = {}
        clashes: Dict[Tuple, int] = {}
        for leaf in rows:
            chain, node = [], leaf
            while node is not None and len(chain) < self.depth:
                chain.append(normalise_place(node.name))
                node = node.parent
            if len(chain) < self.depth or any(part is None for part in chain):
                continue
            key = tuple(reversed(chain))
            if key in index and index[key][0] != leaf.code:
                # The gazetteer itself is ambiguous here. Refuse to guess: drop the
                # entry so callers get "unresolved" rather than an arbitrary winner.
                clashes[key] = clashes.get(key, 1) + 1
                index.pop(key, None)
                continue
            if key not in clashes:
                index[key] = (leaf.code, leaf.name)
        self.ambiguous = clashes
        if clashes:
            logger.warning(
                "api_etl: %s location name path(s) are ambiguous in the loaded tree and "
                "will not resolve: %s", len(clashes),
                ", ".join("/".join(k) for k in list(clashes)[:5]))
        logger.info("api_etl: location index built - %s resolvable path(s)", len(index))
        return index

    @property
    def index(self) -> Dict[Tuple, Tuple[str, str]]:
        if self._index is None:
            self._index = self._build()
        return self._index

    def resolve(self, names: Sequence) -> Optional[Tuple[str, str]]:
        """(code, canonical_name) for this name path, or None."""
        key = tuple(normalise_place(part) for part in names)
        if any(part is None for part in key):
            return None
        return self.index.get(key)

    def __len__(self) -> int:
        return len(self.index)


def apply_aliases(names: Sequence, aliases: Optional[Dict[str, Dict[str, str]]],
                  levels: Sequence[str]) -> List:
    """Rewrite source spellings to the gazetteer's, per level.

    `aliases` is {level_name: {source_spelling: gazetteer_spelling}}, matched on the
    normalised source spelling so the map does not have to repeat punctuation variants.
    """
    if not aliases:
        return list(names)
    out = []
    for level, value in zip(levels, names):
        table = aliases.get(level) or {}
        lookup = {normalise_place(k): v for k, v in table.items()}
        replacement = lookup.get(normalise_place(value))
        out.append(replacement if replacement is not None else value)
    return out
