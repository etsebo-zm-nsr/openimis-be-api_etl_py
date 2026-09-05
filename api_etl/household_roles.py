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
