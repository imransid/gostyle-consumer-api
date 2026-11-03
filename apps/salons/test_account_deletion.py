"""
Day 30 of an account deletion: its bookings still to come are cancelled as
the customer's own cancel (apps/salons/account_deletion.py). booking-api is
faked at the client functions; the token check is the real one.
"""

import urllib.parse
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

from apps.accounts.models import ConsumerAccount, UserSession
from apps.grpc_gen import auth_pb2
from apps.grpc_gen.servicer import ConsumerAuthServicer
from apps.salons import account_deletion as ad
from apps.salons.booking_api import BookingApiUnavailable


def page_of(rows, count=None):
    return 200, {"count": len(rows) if count is None else count, "results": rows,
                 "counts": {"upcoming": len(rows), "recurring": 0, "archive": 0}}


class FakeBookingApi:
    """The four client calls the cancel makes, with a log of them in order."""

    def __init__(self, recurring=(), upcoming=(), answers=None):
        self.shelves = {"recurring": list(recurring), "upcoming": list(upcoming)}
        self.answers = answers or {}
        self.log = []

    def list_bookings(self, query, *, authorization, tenant_id=None, timeout=None):
        params = dict(urllib.parse.parse_qsl(query))
        self.log.append(("list", params["filter"], int(params["page"])))
        rows = self.shelves[params["filter"]]
        size = int(params["pageSize"])
        start = (int(params["page"]) - 1) * size
        return page_of(rows[start:start + size], count=len(rows))

    def _answer(self, kind, row_id):
        self.log.append((kind, row_id))
        return self.answers.get(row_id, (200, {}))

    def cancel_booking(self, booking_id, body, *, authorization, idempotency_key=None):
        self.log.append(("body", booking_id, body))
        return self._answer("booking", booking_id)

    def cancel_group_booking(self, group_id, *, authorization, tenant_id=None):
        return self._answer("party", group_id)

    def cancel_series_booking(self, series_id, body, *, authorization, idempotency_key=None):
        self.log.append(("body", series_id, body))
        return self._answer("routine", series_id)

    def patch(self, test):
        for name in ("list_bookings", "cancel_booking", "cancel_group_booking",
                     "cancel_series_booking"):
            patcher = mock.patch.object(ad, name, side_effect=getattr(self, name))
            patcher.start()
            test.addCleanup(patcher.stop)
        return self


class CancelBookingsToComeTests(TestCase):
    def setUp(self):
        self.account = ConsumerAccount.objects.create(phone="+8801712345678", full_name="Kevin")

    def run_with(self, **shelves):
        api = FakeBookingApi(**shelves).patch(self)
        return ad.cancel_bookings_to_come(self.account), api

    def cancels(self, api):
        return [entry for entry in api.log if entry[0] in ("booking", "party", "routine")]

    def test_routines_first_then_bookings_and_the_bookers_parties(self):
        report, api = self.run_with(
            recurring=[
                {"id": "r1", "booking_type": "ROUTINE", "status": "ACTIVE"},
                {"id": "r2", "booking_type": "ROUTINE", "status": "PAUSED"},
                {"id": "r3", "booking_type": "ROUTINE", "status": "ENDED"},
                {"id": "r4", "booking_type": "ROUTINE", "status": "COMPLETED"},
            ],
            upcoming=[
                {"id": "b1", "booking_type": "SINGLE"},
                {"id": "g1", "booking_type": "GROUP", "is_booker": True},
                {"id": "g2", "booking_type": "GROUP", "is_booker": False},
                {"id": "v1", "booking_type": "SINGLE", "series_id": "r9"},
            ],
        )

        self.assertEqual(
            self.cancels(api),
            [("routine", "r1"), ("routine", "r2"), ("booking", "b1"), ("party", "g1")],
        )
        self.assertIn(("body", "r1", {"reason": "OTHER"}), api.log)
        self.assertIn(("body", "b1", {"reason": ad.CANCEL_TEXT}), api.log)
        self.assertEqual(report.cancelled, 4)
        self.assertEqual(
            report.left,
            [("party", "g2", "a place in someone else's party"),
             ("routine visit", "v1", "its routine was not cancelled")],
        )

    def test_a_refusal_is_left_and_the_rest_go_on(self):
        api = FakeBookingApi(
            upcoming=[{"id": "b1", "booking_type": "SINGLE"}, {"id": "b2", "booking_type": "SINGLE"}],
            answers={"b1": (409, {"code": "cannot_cancel"})},
        ).patch(self)

        report = ad.cancel_bookings_to_come(self.account)

        self.assertEqual(self.cancels(api), [("booking", "b1"), ("booking", "b2")])
        self.assertEqual(report.cancelled, 1)
        self.assertEqual(report.left, [("booking", "b1", "409 cannot_cancel")])

    def test_a_party_with_no_is_booker_is_tried_and_a_404_left(self):
        api = FakeBookingApi(
            upcoming=[{"id": "g1", "booking_type": "GROUP"}],
            answers={"g1": (404, {"code": "NOT_FOUND"})},
        ).patch(self)

        report = ad.cancel_bookings_to_come(self.account)

        self.assertEqual(self.cancels(api), [("party", "g1")])
        self.assertEqual(report.left, [("party", "g1", "404 NOT_FOUND")])

    def test_a_5xx_is_unreachable(self):
        FakeBookingApi(
            upcoming=[{"id": "b1", "booking_type": "SINGLE"}],
            answers={"b1": (503, {"code": "DEPENDENCY_UNAVAILABLE"})},
        ).patch(self)

        with self.assertRaises(ad.Unreachable):
            ad.cancel_bookings_to_come(self.account)

    def test_booking_api_down_is_unreachable(self):
        with mock.patch.object(ad, "list_bookings", side_effect=BookingApiUnavailable("refused")):
            with self.assertRaises(ad.Unreachable):
                ad.cancel_bookings_to_come(self.account)

    def test_a_list_it_will_not_give_is_unreachable(self):
        with mock.patch.object(ad, "list_bookings", return_value=(401, {"code": "UNAUTHENTICATED"})):
            with self.assertRaises(ad.Unreachable):
                ad.cancel_bookings_to_come(self.account)

    def test_every_page_is_read_before_anything_is_cancelled(self):
        rows = [{"id": f"b{i}", "booking_type": "SINGLE"} for i in range(60)]
        report, api = self.run_with(upcoming=rows)

        lists = [i for i, entry in enumerate(api.log) if entry[0] == "list"]
        first_cancel = next(i for i, entry in enumerate(api.log) if entry[0] == "booking")
        self.assertEqual(
            [api.log[i] for i in lists],
            [("list", "recurring", 1), ("list", "upcoming", 1), ("list", "upcoming", 2)],
        )
        self.assertLess(lists[-1], first_cancel)
        self.assertEqual(report.cancelled, 60)

    def test_nothing_to_cancel(self):
        report, api = self.run_with()

        self.assertEqual(self.cancels(api), [])
        self.assertEqual((report.cancelled, report.left), (0, []))


@override_settings(DEVICE_SESSIONS_V1=True)
class TheTokenTests(TestCase):
    """booking-api checks a customer token with VerifyConsumer. The purge's
    token must pass it while the cancel runs, and nothing after."""

    def setUp(self):
        self.account = ConsumerAccount.objects.create(
            phone="+8801712345678", email="kevin@example.com", full_name="Kevin"
        )

    def verify(self, authorization):
        token = authorization.removeprefix("Bearer ")
        return ConsumerAuthServicer()._verify(auth_pb2.VerifyConsumerRequest(token=token))

    def test_the_customers_own_token_while_it_runs_and_dead_after(self):
        seen = {}

        def list_bookings(query, *, authorization, **kwargs):
            seen["authorization"] = authorization
            seen["answer"] = self.verify(authorization)
            return page_of([])

        with mock.patch.object(ad, "list_bookings", side_effect=list_bookings):
            ad.cancel_bookings_to_come(self.account)

        self.assertTrue(seen["answer"].valid)
        self.assertEqual(seen["answer"].consumer_id, str(self.account.pk))
        self.assertFalse(self.verify(seen["authorization"]).valid)
        session = UserSession.objects.get(user=self.account)
        self.assertEqual(session.device_name, ad.SESSION_NAME)
        self.assertIsNotNone(session.ended_at)
        # No "new sign-in" email: nobody signed in.
        self.assertEqual(mail.outbox, [])

    def test_the_session_ends_when_booking_api_is_down_too(self):
        with mock.patch.object(ad, "list_bookings", side_effect=BookingApiUnavailable("down")):
            with self.assertRaises(ad.Unreachable):
                ad.cancel_bookings_to_come(self.account)

        self.assertIsNotNone(UserSession.objects.get(user=self.account).ended_at)
