"""
A party's Cancel button (GROUP_CANCEL_LANES_V1): can_cancel on a GROUP row of
My Bookings is false once any member of the party is checked in, in service
or finished, because the party cancel is all or nothing (booking-api answers
409 cannot_cancel then). Worked out from the party's member rows, read once
per GROUP row the window would let cancel, never from the party's status.
Off by default: can_cancel exactly as before, and no party is read.
"""

from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.salons.booking_api import BookingApiUnavailable
from apps.salons.test_group_booking import Customer
from apps.salons.test_group_self_check_in import setting_in, without
from apps.salons import tests as list_tests
from apps.salons.views import BookingListView

FLAG = "GROUP_CANCEL_LANES_V1"
GROUP = "aaaaaaaa-0000-4aaa-8aaa-aaaaaaaaaaaa"


def member(ref, status="CONFIRMED_BY_SALON", kind="registered"):
    return {"ref": ref, "id": f"p{ref}", "kind": kind, "status": status}


def party(*statuses):
    """The party read: the booker first, then one member per status."""
    booker, *rest = statuses
    return {
        "id": GROUP,
        "booking_type": "GROUP",
        "status": "CONFIRMED_BY_SALON",
        "members": [member(0, booker, "self")]
        + [member(i + 1, s) for i, s in enumerate(rest)],
    }


class SwitchTests(SimpleTestCase):

    def test_the_flag_is_off_by_default(self):
        self.assertIn(f"{FLAG}=false", Path(".env.example").read_text())
        self.assertIn(f"{FLAG}: ${{{FLAG}:-False}}", Path("docker-compose.yml").read_text())
        source = Path("config/settings/base.py").read_text()
        self.assertIn(f'{FLAG} = env.bool("{FLAG}", default=False)', source)

    def test_unset_means_off(self):
        self.assertIs(setting_in(FLAG, without(FLAG)), False)

    def test_true_turns_it_on_and_a_typo_does_not(self):
        self.assertIs(setting_in(FLAG, {**without(FLAG), FLAG: "true"}), True)
        self.assertIs(setting_in(FLAG, {**without(FLAG), FLAG: "ture"}), False)


class GroupCancelButtonTests(SimpleTestCase):
    """
    The list endpoint with booking-api's page, the salon lookup and the
    party read replaced. The window is open on every row here (two days out,
    a 24 hour window), so any `false` is the members' doing.
    """

    # The list's own fixtures, borrowed: the module, not the class, is
    # imported, or the runner would collect the class here a second time.
    NOW = list_tests.BookingListViewTests.NOW
    CARD = list_tests.BookingListViewTests.CARD
    row = list_tests.BookingListViewTests.row
    page = list_tests.BookingListViewTests.page

    def get(self, rows, *, read=None):
        """The list, and the party reads it made. `read` answers each one."""
        request = APIRequestFactory().get("/api/v1/bookings", HTTP_AUTHORIZATION="Bearer cus")
        force_authenticate(request, user=Customer())
        if read is None:
            read = (200, party("CONFIRMED_BY_SALON", "CONFIRMED_BY_SALON"))
        reader = mock.Mock(
            side_effect=read if isinstance(read, Exception) else None,
            return_value=None if isinstance(read, Exception) else read,
        )
        with mock.patch(
            "apps.salons.views.list_bookings", return_value=(200, self.page(rows)),
        ), mock.patch(
            "apps.salons.views.salon_cards_for_refs",
            return_value={"marina-walk": self.CARD},
        ), mock.patch("apps.salons.views.datetime") as clock, mock.patch(
            "apps.salons.views.read_group_booking", reader,
        ):
            clock.now.return_value = self.NOW
            clock.fromisoformat = __import__("datetime").datetime.fromisoformat
            response = BookingListView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        return response.data["results"], reader

    def group_row(self, **over):
        return self.row(id=GROUP, booking_type="GROUP", **over)

    # ------------------------------------------------------------- off

    def test_off_the_button_is_the_window_s_and_no_party_is_read(self):
        rows, reader = self.get(
            [self.group_row()], read=(200, party("CHECKED_IN", "CONFIRMED_BY_SALON")),
        )
        self.assertIs(rows[0]["can_cancel"], True)
        reader.assert_not_called()

    # -------------------------------------------------------------- on

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_nobody_checked_in_the_button_stays(self):
        rows, reader = self.get([self.group_row()])
        self.assertIs(rows[0]["can_cancel"], True)
        reader.assert_called_once()

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_the_booker_checked_in_while_the_party_reads_confirmed_no_button(self):
        # booking-api's MOBILE_GROUP_LIVE_STATUS: the GROUP row says
        # CONFIRMED_BY_SALON because another lane still is, and the booker's
        # lane is CHECKED_IN. The party cancel would answer 409.
        rows, _ = self.get(
            [self.group_row(status="CONFIRMED_BY_SALON")],
            read=(200, party("CHECKED_IN", "CONFIRMED_BY_SALON", "CONFIRMED_BY_SALON")),
        )
        self.assertIs(rows[0]["can_cancel"], False)

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_another_member_checked_in_no_button_whoever_the_booker_is(self):
        rows, _ = self.get(
            [self.group_row()],
            read=(200, party("CONFIRMED_BY_SALON", "CONFIRMED_BY_SALON", "CHECKED_IN")),
        )
        self.assertIs(rows[0]["can_cancel"], False)

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_a_finished_member_no_button(self):
        rows, _ = self.get(
            [self.group_row()], read=(200, party("CONFIRMED_BY_SALON", "COMPLETED")),
        )
        self.assertIs(rows[0]["can_cancel"], False)

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_a_cancelled_member_does_not_block_the_rest(self):
        # The party cancel skips a member already gone, so the button stays.
        rows, _ = self.get(
            [self.group_row()], read=(200, party("CONFIRMED_BY_SALON", "CANCELLED")),
        )
        self.assertIs(rows[0]["can_cancel"], True)

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_a_read_that_does_not_answer_is_no_button(self):
        for read in (
            BookingApiUnavailable("down"),
            (404, {"code": "not_found"}),
            (503, None),
            (200, {"id": GROUP}),
            (200, {"members": "nope"}),
        ):
            with self.subTest(read=read):
                rows, _ = self.get([self.group_row()], read=read)
                self.assertIs(rows[0]["can_cancel"], False)

    @override_settings(GROUP_CANCEL_LANES_V1=True, SINGLE_LOOKUP_TIMEOUT=2)
    def test_the_party_is_read_with_the_caller_s_token_and_the_short_timeout(self):
        _, reader = self.get([self.group_row()])
        reader.assert_called_once_with(GROUP, authorization="Bearer cus", timeout=2)

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_only_a_group_row_the_window_lets_cancel_is_read(self):
        late = {"cancel_window_hours": 24 * 30}
        with mock.patch.dict(self.CARD, late):
            rows, reader = self.get([self.group_row()])
        self.assertIs(rows[0]["can_cancel"], False)
        reader.assert_not_called()

        rows, reader = self.get([self.group_row(status="CHECKED_IN")])
        self.assertIs(rows[0]["can_cancel"], False)
        reader.assert_not_called()

    @override_settings(GROUP_CANCEL_LANES_V1=True, SINGLE_BOOKING_ACTIONS_V1=True)
    def test_a_single_row_is_never_read_and_keeps_its_button(self):
        rows, reader = self.get([self.row()])
        self.assertIs(rows[0]["can_cancel"], True)
        reader.assert_not_called()

    @override_settings(GROUP_CANCEL_LANES_V1=True)
    def test_one_read_per_group_row_and_each_row_its_own_answer(self):
        other = "bbbbbbbb-0000-4bbb-8bbb-bbbbbbbbbbbb"
        answers = {
            GROUP: (200, party("CONFIRMED_BY_SALON", "CHECKED_IN")),
            other: (200, party("CONFIRMED_BY_SALON", "CONFIRMED_BY_SALON")),
        }
        request = APIRequestFactory().get("/api/v1/bookings", HTTP_AUTHORIZATION="Bearer cus")
        force_authenticate(request, user=Customer())
        rows = [self.group_row(), self.row(id=other, booking_type="GROUP")]
        with mock.patch(
            "apps.salons.views.list_bookings", return_value=(200, self.page(rows)),
        ), mock.patch(
            "apps.salons.views.salon_cards_for_refs",
            return_value={"marina-walk": self.CARD},
        ), mock.patch("apps.salons.views.datetime") as clock, mock.patch(
            "apps.salons.views.read_group_booking",
            side_effect=lambda group_id, **_: answers[group_id],
        ) as reader:
            clock.now.return_value = self.NOW
            clock.fromisoformat = __import__("datetime").datetime.fromisoformat
            results = BookingListView.as_view()(request).data["results"]
        self.assertEqual([r["can_cancel"] for r in results], [False, True])
        self.assertEqual(reader.call_count, 2)
