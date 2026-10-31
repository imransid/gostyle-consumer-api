"""
Every kind of notification a customer can get, and its rules.

The one place that says which tab an item sits in, which of the customer's
switches silences its alert, and which channels it may go out on. service.py
reads it to decide; nothing else holds these rules, so a new kind is one line
here and no migration.

Kinds listed here before anything produces them (booking, payment, offers)
are the contract the later producers will send against
(docs/NOTIFICATIONS_SETTINGS_AUDIT.md, phases 4 to 8).
"""

from dataclasses import dataclass
from datetime import timedelta

from .models import Category, NotificationDelivery

PUSH = NotificationDelivery.Channel.PUSH
EMAIL = NotificationDelivery.Channel.EMAIL


class UnknownKind(ValueError):
    """A type not in KINDS: a producer bug, never retried."""


@dataclass(frozen=True)
class Kind:
    category: str
    # The channels it may use. SMS is in none of them until a provider is
    # chosen (decision D8).
    channels: tuple
    # The NotificationPreference switch that silences its alert, or None for
    # an alert the customer always gets (money, account safety). Their own
    # channel switches (push, ...) still apply either way.
    preference: str | None
    # Promotional kinds are not even kept unless their switch is on. Every
    # other kind is always kept: the switches decide the alert, not the
    # history (decision D2).
    promotional: bool = False
    # The push's data["type"]. Set for kinds that went out before the
    # Notification Center, so an app build already in customers' hands keeps
    # recognising them. None: the kind's own key.
    push_type: str | None = None
    # How long an alert may wait to go out. A producer with a hard end (a
    # reminder ends at the visit) passes its own.
    deliver_within: timedelta = timedelta(days=1)


KINDS = {
    # Bookings. A check-in answer is not silenced by "Booking changes": it
    # answers the customer's own tap at the desk.
    "booking.confirmed": Kind(Category.ORDERS, (PUSH, EMAIL), "booking_confirmation"),
    "booking.rescheduled": Kind(Category.ORDERS, (PUSH, EMAIL), "booking_changes"),
    "booking.cancelled": Kind(Category.ORDERS, (PUSH, EMAIL), "booking_changes"),
    "check_in.approved": Kind(Category.ORDERS, (PUSH,), None),
    "check_in.rejected": Kind(Category.ORDERS, (PUSH,), None),
    "check_in.expired": Kind(Category.ORDERS, (PUSH,), None),
    "booking.reminder": Kind(Category.UPDATES, (PUSH, EMAIL), "appointment_reminder"),
    # Money.
    "payment.completed": Kind(Category.PAYMENTS, (PUSH, EMAIL), None),
    "payment.refunded": Kind(Category.PAYMENTS, (PUSH, EMAIL), None),
    # The account. The sign-in alert's email still goes the way it always
    # did (notifications.send_new_sign_in_notice); only its push comes here.
    "account.new_sign_in": Kind(
        Category.UPDATES, (PUSH,), None, push_type="new_sign_in"
    ),
    # Promotions.
    "promo.offer": Kind(Category.PROMOTIONS, (PUSH, EMAIL), "special_offers", promotional=True),
    "promo.new_salon": Kind(Category.PROMOTIONS, (PUSH,), "new_salons", promotional=True),
    "promo.loyalty": Kind(Category.PROMOTIONS, (PUSH, EMAIL), "loyalty", promotional=True),
    "salon.new_styles": Kind(Category.UPDATES, (PUSH,), "special_offers", promotional=True),
}


def kind_of(type_):
    try:
        return KINDS[type_]
    except KeyError:
        raise UnknownKind(type_) from None
