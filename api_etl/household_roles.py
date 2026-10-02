"""Restore household roles and the primary recipient after upstream grouping.

Upstream builds a group's members one at a time, and every `GroupIndividual.save()`
runs `GroupAndGroupIndividualAlignmentService`. While the group is still being
assembled it has no head, so `_assure_primary_recipient_in_group` promotes whichever
member happens to be created first to HEAD + PRIMARY. When the real head is created a
moment later, `_change_head` sets the stand-in's role to None - not back to the role it
arrived with - and the stand-in keeps PRIMARY.

Two things are wrong afterwards, and neither reports an error:

  * one member per household has no relationship to the head (34 of 50 households on a
    200-record ZISPIS batch);
  * the primary recipient is a son or a grandchild rather than the head. A
    "exactly one PRIMARY per household" check still passes, so this hides.

The order members are created in is `ArrayAgg('id')` over randomly generated UUIDs, so
it cannot be influenced from the emitting side - the ordering has to be repaired after
the fact.

This reads back the durable `household_role` the adapter wrote onto each individual and
restores both fields. Writes go through `update()` rather than `save()` on purpose: the
alignment cascade is what corrupted the data, and re-entering it would undo the repair.
"""
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

HEAD = "HEAD"
PRIMARY = "PRIMARY"


def _normalise_recipient(value):
    """Canonical string for a `household_recipient` marker.

    None/'' -> None; a number (1, 1.0, "1.0" - what a pandas round trip of a column
    mixing blanks and "1" produces) -> its integer string when whole; anything else ->
    the stripped string.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number == int(number) else text


def role_value(name):
    """Map a Role ATTRIBUTE name to the value the column stores.

    They are not the same string for every role: `Role.OTHER_RELATIVE` is stored as
    "OTHER RELATIVE", and `NOT_RELATED` as "NOT RELATED". Adapters emit attribute names
    because that is what `_individual_role_parser` expects, so writing one straight into
    the column would store a value outside the field's choices - invisible until
    something asks for its display name.
    """
    from individual.models import GroupIndividual

    if not name:
        return None
    return getattr(GroupIndividual.Role, str(name).strip().upper(), None)


def reconcile_household_roles(group_codes=None, dry_run=False):
    """Repair roles and recipient for the given group codes (all ETL groups if None).

    Returns a summary dict. Idempotent - a second run reports zero changes.
    """
    from individual.models import Group, GroupIndividual

    groups = Group.objects.filter(is_deleted=False)
    if group_codes:
        groups = groups.filter(code__in=list(group_codes))

    summary = {"groups": 0, "roles_restored": 0, "recipients_moved": 0, "no_source": 0}

    for group in groups.iterator():
        members = list(
            GroupIndividual.objects.filter(group=group, is_deleted=False)
            .select_related("individual")
        )
        intended = {}
        for member in members:
            role = role_value((member.individual.json_ext or {}).get("household_role"))
            if role:
                intended[member.id] = role
        if not intended:
            # Not an ETL-sourced household, or imported before household_role existed.
            summary["no_source"] += 1
            continue

        summary["groups"] += 1
        wrong_role = [m for m in members
                      if m.id in intended and m.role != intended[m.id]]
        heads = [m for m in members if intended.get(m.id) == role_value(HEAD)]
        wrong_recipient = []
        if heads:
            head = heads[0]
            if head.recipient_type != PRIMARY:
                wrong_recipient = [m for m in members
                                   if m.recipient_type == PRIMARY and m.id != head.id]
                wrong_recipient.append(head)

        if dry_run:
            summary["roles_restored"] += len(wrong_role)
            summary["recipients_moved"] += 1 if wrong_recipient else 0
            continue

        for member in wrong_role:
            # .update() bypasses GroupIndividual.save(), and must: that save runs the
            # alignment cascade that caused this.
            GroupIndividual.objects.filter(pk=member.pk).update(role=intended[member.id])
            summary["roles_restored"] += 1

        if wrong_recipient:
            head = wrong_recipient[-1]
            for member in wrong_recipient[:-1]:
                GroupIndividual.objects.filter(pk=member.pk).update(recipient_type=None)
            GroupIndividual.objects.filter(pk=head.pk).update(recipient_type=PRIMARY)
            summary["recipients_moved"] += 1

    if summary["roles_restored"] or summary["recipients_moved"]:
        logger.warning(
            "api_etl: repaired household grouping - %s role(s) restored, %s "
            "household(s) had the wrong primary recipient, across %s household(s)%s",
            summary["roles_restored"], summary["recipients_moved"], summary["groups"],
            " (dry run)" if dry_run else "",
        )
    return summary


def flag_household_head_status(group_codes=None, dry_run=False):
    """Record on each household whether its head is active and who declared the recipient.

    Real households often have no active head: the head the source names may be
    SUSPENDED or GRADUATED, or there may be no head at all. The business rule is to never
    create or remove a head and to keep the one the source named, so the household is
    FLAGGED instead. Two keys are merged into `Group.json_ext`:

      * `head_status` - the head individual's `json_ext["status"]` upper-cased, "UNKNOWN"
        if the head has none, or "NO_HEAD" if the household has no HEAD member. With
        several heads the lowest individual id is used and `multiple_heads` is counted.
      * `primary_recipient_source` - "declared" if the PRIMARY member's individual carries
        `household_recipient` equal to the source's primary marker, "auto_assigned" if
        there is a PRIMARY the source did not declare (upstream promoted someone), "none"
        if nobody is PRIMARY.

    Same selection as `reconcile_household_roles`: non-deleted groups, and households
    where no member carries an ETL `household_role` are skipped.

    Upstream trap: `GroupAndGroupIndividualAlignmentService.update_json_ext_for_group`
    runs on every GroupIndividual save and rewrites the group's json_ext so its
    non-reserved keys mirror the HEAD individual's json_ext, deleting other keys. Any
    later member save can therefore wipe these flags. This function is idempotent and is
    meant to be re-run after every import. Writes use `update()`, never `save()`, and no
    role or recipient is ever changed here.

    Returns a summary dict: counts per head_status value under "head_status", per
    source under "primary_recipient_source", plus "groups", "multiple_heads", "no_source"
    and "changed".
    """
    from api_etl.adapters.base_mapping_adapter import RECIPIENT_PRIMARY
    from individual.models import Group, GroupIndividual

    groups = Group.objects.filter(is_deleted=False)
    if group_codes:
        groups = groups.filter(code__in=list(group_codes))

    summary = {"groups": 0, "no_source": 0, "multiple_heads": 0, "changed": 0,
               "head_status": defaultdict(int),
               "primary_recipient_source": defaultdict(int)}

    for group in groups.iterator():
        members = list(
            GroupIndividual.objects.filter(group=group, is_deleted=False)
            .select_related("individual")
        )
        if not any((m.individual.json_ext or {}).get("household_role") for m in members):
            summary["no_source"] += 1
            continue
        summary["groups"] += 1

        heads = sorted((m for m in members if m.role == HEAD),
                       key=lambda m: str(m.individual_id))
        if heads:
            if len(heads) > 1:
                summary["multiple_heads"] += 1
            status = (heads[0].individual.json_ext or {}).get("status")
            head_status = str(status).strip().upper() if status else ""
            head_status = head_status or "UNKNOWN"
        else:
            head_status = "NO_HEAD"

        primary = next((m for m in members if m.recipient_type == PRIMARY), None)
        if primary is None:
            recipient_source = "none"
        elif _normalise_recipient((primary.individual.json_ext or {}).get("household_recipient")) \
                == _normalise_recipient(RECIPIENT_PRIMARY):
            recipient_source = "declared"
        else:
            recipient_source = "auto_assigned"

        summary["head_status"][head_status] += 1
        summary["primary_recipient_source"][recipient_source] += 1

        current = dict(group.json_ext or {})
        wanted = {"head_status": head_status, "primary_recipient_source": recipient_source}
        if all(current.get(k) == v for k, v in wanted.items()):
            continue
        summary["changed"] += 1
        if not dry_run:
            current.update(wanted)
            Group.objects.filter(pk=group.pk).update(json_ext=current)

    summary["head_status"] = dict(summary["head_status"])
    summary["primary_recipient_source"] = dict(summary["primary_recipient_source"])
    return summary
