"""Tests for the post-grouping household repair.

The bug being repaired is upstream and silent: while a group is being assembled it has
no head, so `_assure_primary_recipient_in_group` promotes whichever member is created
first to HEAD + PRIMARY; when the real head arrives `_change_head` sets the stand-in's
role to None and the stand-in keeps PRIMARY.

Both halves matter. The lost role costs a household relationship; the misplaced PRIMARY
means a son rather than the head is the payment recipient, and a "one PRIMARY per
household" check still passes, so nothing surfaces it.
"""
from django.test import TestCase

from api_etl.household_roles import reconcile_household_roles
from core.models import User
from individual.models import Group, GroupIndividual, Individual


class ReconcileHouseholdRolesTestCase(TestCase):

    def setUp(self):
        self.user = User.objects.filter(username="Admin").first() or User.objects.create(
            username="test_etl_roles")
        self.group = Group(code="TEST-HH-1")
        self.group.save(user=self.user)

    def _member(self, name, household_role, role, recipient_type=None):
        individual = Individual(first_name=name, last_name="Banda", dob="1990-01-01",
                                json_ext={"household_role": household_role})
        individual.save(user=self.user)
        member = GroupIndividual(group=self.group, individual=individual)
        member.save(user=self.user)
        # save() runs the alignment cascade; set the damaged state directly, which is
        # what the cascade leaves behind.
        GroupIndividual.objects.filter(pk=member.pk).update(
            role=role, recipient_type=recipient_type)
        return GroupIndividual.objects.get(pk=member.pk)

    def _state(self):
        return {m.individual.first_name: (m.role, m.recipient_type)
                for m in GroupIndividual.objects.filter(group=self.group, is_deleted=False)}

    def test_restores_the_role_the_stand_in_lost(self):
        self._member("Junior", "SON", role=None, recipient_type="PRIMARY")
        self._member("Mary", "HEAD", role="HEAD", recipient_type=None)
        summary = reconcile_household_roles(group_codes=["TEST-HH-1"])
        self.assertEqual(summary["roles_restored"], 1)
        self.assertEqual(self._state()["Junior"][0], "SON")

    def test_moves_primary_recipient_back_to_the_head(self):
        self._member("Junior", "SON", role=None, recipient_type="PRIMARY")
        self._member("Mary", "HEAD", role="HEAD", recipient_type=None)
        reconcile_household_roles(group_codes=["TEST-HH-1"])
        state = self._state()
        self.assertEqual(state["Mary"][1], "PRIMARY")
        self.assertIsNone(state["Junior"][1])

    def test_is_idempotent(self):
        self._member("Junior", "SON", role=None, recipient_type="PRIMARY")
        self._member("Mary", "HEAD", role="HEAD", recipient_type=None)
        reconcile_household_roles(group_codes=["TEST-HH-1"])
        again = reconcile_household_roles(group_codes=["TEST-HH-1"])
        self.assertEqual(again["roles_restored"], 0)
        self.assertEqual(again["recipients_moved"], 0)

    def test_healthy_household_is_left_alone(self):
        self._member("Mary", "HEAD", role="HEAD", recipient_type="PRIMARY")
        self._member("Junior", "SON", role="SON")
        summary = reconcile_household_roles(group_codes=["TEST-HH-1"])
        self.assertEqual(summary["roles_restored"], 0)
        self.assertEqual(summary["recipients_moved"], 0)

    def test_dry_run_writes_nothing(self):
        self._member("Junior", "SON", role=None, recipient_type="PRIMARY")
        self._member("Mary", "HEAD", role="HEAD", recipient_type=None)
        summary = reconcile_household_roles(group_codes=["TEST-HH-1"], dry_run=True)
        self.assertEqual(summary["roles_restored"], 1)
        self.assertIsNone(self._state()["Junior"][0])

    def test_non_etl_household_is_skipped_not_blanked(self):
        """A group whose members carry no household_role must not be touched."""
        individual = Individual(first_name="Other", last_name="Person",
                                dob="1990-01-01", json_ext={})
        individual.save(user=self.user)
        member = GroupIndividual(group=self.group, individual=individual)
        member.save(user=self.user)
        GroupIndividual.objects.filter(pk=member.pk).update(role="SPOUSE")
        summary = reconcile_household_roles(group_codes=["TEST-HH-1"])
        self.assertEqual(summary["groups"], 0)
        self.assertEqual(summary["no_source"], 1)
        self.assertEqual(self._state()["Other"][0], "SPOUSE")
