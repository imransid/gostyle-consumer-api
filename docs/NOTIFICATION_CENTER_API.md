# Notification Center (FE Integration Guide)

> **Live on production. Build from `docs/NOTIFICATIONS_FE_GUIDE.md`**, the
> one guide for the whole feature. This page is the first version, kept for
> its history.

Date: 2026-10-31. For: the mobile app team. The Notification Center screen
(tabs All, Orders, Updates, Payments, Promotions) and the bell's unread
badge.

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>` on every call.

---

## 0. Read this first

1. **Behind a server switch, `NOTIFICATION_CENTER_V1`.** While it is off,
   every route on this page answers `404`. We tell you when it is on.
2. **What fills it, and when.** New sign-in alerts, and (with booking-api's
   `MOBILE_NOTIFY_HUB` on) booking confirmed / moved / cancelled, written
   with the salon's name and the time on the salon's clock ("Your booking
   GS-1481 at Demo Salon & Spa on Mon 2 Nov at 11:00 AM is confirmed.").
   With booking-api's `MOBILE_REMINDER_CHOICE` on too: the appointment
   reminder, once per visit, at the time chosen on the preferences screen.
   Then, as each part ships: check-in answers, payments, offers. Nothing changes on your
   side when a new kind arrives: same item shape, its own `type`.
3. **Nothing is seeded.** A new customer, or one with no bookings, sees an
   empty list: `200` with `"results": []`, and unread `0`. Show your empty
   state.
4. **The switches on the preferences screen decide the alert, not the
   history.** "Booking confirmation" off means no push for it; the booking
   is still in the center. Promotions are the exception: with "Special
   offers" off, an offer is not kept at all.
5. **Group by day on the phone.** Today, Yesterday, This week, Earlier:
   from `created_at`, in the phone's own time zone. The server does not
   know it.
6. Validation errors are `422`, as everywhere in this API except
   `/notifications/preferences` (that one keeps its `400`).

---

## 1. The list

```http
GET /notifications?category=all&page_size=20
Authorization: Bearer <access_token>
```

| Param | Values | Default |
| --- | --- | --- |
| `category` | `all`, `orders`, `updates`, `payments`, `promotions` | `all` |
| `unread` | `true` (unread only), `false` (read only) | both |
| `page_size` | 1 to 50 | 20 |
| `cursor` | the value inside `next` | first page |

Newest first. **Paged by cursor, not page number**: call the `next` URL as
it is, until it is `null`. A notification that arrives while the customer
scrolls does not push items onto the next page twice.

`200`:

```json
{
  "next": "https://api.gostyle.uk/api/v1/notifications?cursor=cD0yMDI2LTEwLTMxKzA4JTNBMjA%3D&page_size=20",
  "previous": null,
  "results": [
    {
      "id": "6b1f0c3e-2d4a-4b8e-9c71-5a0e8f2d3b19",
      "category": "updates",
      "type": "account.new_sign_in",
      "title": "New sign-in to your account",
      "body": "Your Go Style account was just signed in to on iPhone 15 in Dubai, UAE. If this was not you, change your password.",
      "is_read": false,
      "read_at": null,
      "created_at": "2026-10-31T08:20:36.365000Z",
      "occurred_at": "2026-10-31T08:20:36.365000Z",
      "target": {"type": "session", "id": "be256a60-25a2-4804-8e92-9e6875b369ea"},
      "data": {"sessionId": "be256a60-25a2-4804-8e92-9e6875b369ea"}
    }
  ]
}
```

## 2. One item

```http
GET /notifications/{id}
```

The same item. **Does not mark it read**: call section 3 when the customer
opens it. `404` for an id that is not theirs, unknown, or not an id.

## 3. Mark one read

```http
POST /notifications/{id}/read
```

No body. `200` with the item, `is_read: true`. Safe to repeat: the first
`read_at` stays.

## 4. Mark all read

```http
POST /notifications/read-all
Content-Type: application/json

{"category": "all", "before": "2026-10-31T08:20:36.365000Z"}
```

Both fields optional.

- `category`: one tab only, or `all` (default).
- `before`: send the `created_at` of the **newest item on screen**. Only
  items created at or before it are marked, so one that arrived while the
  customer tapped stays unread.

`200`:

```json
{"updated": 4, "unread": {"total": 0, "orders": 0, "updates": 0, "payments": 0, "promotions": 0}}
```

Use `unread` to redraw the badges; no second call needed. More than 30 a
minute answers `429`.

## 5. Unread count (the bell badge)

```http
GET /notifications/unread-count
```

```json
{"total": 3, "orders": 1, "updates": 2, "payments": 0, "promotions": 0}
```

Cheap. Call it when the app comes to the front, and when a push arrives
(section 7).

## 6. The item

| Field | Meaning |
| --- | --- |
| `id` | uuid |
| `category` | its tab: `orders`, `updates`, `payments`, `promotions` |
| `type` | what happened (table below). Pick the icon from it. Show an unknown `type` with a plain icon: new ones will come. |
| `title`, `body` | the words, as the customer was told them. Show as they are. |
| `is_read`, `read_at` | read or not, and when |
| `created_at` | when it reached the center. Sort and group by this. |
| `occurred_at` | when the thing happened (usually the same) |
| `target` | where a tap goes: `{"type", "id"}`, or `null` for nowhere |
| `data` | extra strings for that screen, by `type` (for example `sessionId`) |

| `type` | Tab | `target.type` | When |
| --- | --- | --- | --- |
| `account.new_sign_in` | updates | `session` | a sign-in from a new device (Where you're signed in) |
| `booking.confirmed` | orders | `booking` | a booking is confirmed (for one paid in the app: once the payment is recorded). ONE item for a whole routine ("Routine confirmed", `data.seriesId`) or party ("Your group booking ...", `data.groupId`), not one per visit |
| `booking.rescheduled` | orders | `booking` | a booking moved to a new time |
| `booking.cancelled` | orders | `booking` | a booking was cancelled |
| `check_in.approved`, `check_in.rejected`, `check_in.expired` | orders | `booking` | the desk's answer to "I am here", in the words the check-in push always used ("You are checked in", "Sara A. is checked in"). Not silenced by "Booking changes": it answers the customer's own tap |
| `booking.reminder` | updates | `booking` | the appointment reminder, at the customer's chosen time ("Your booking GS-1483 at Demo Salon & Spa is today at 11:30 AM.") |
| `payment.completed`, `payment.refunded` | payments | `booking` | coming |
| `promo.offer`, `promo.new_salon`, `promo.loyalty`, `salon.new_styles` | promotions / updates | `offer` or `salon` | coming |

## 7. Pushes and keeping the badge right

Every push that comes from the center carries, in its `data` (all strings):

| Key | Example |
| --- | --- |
| `notificationId` | `6b1f0c3e-...` (the item's `id`) |
| `type` | the item's `type`, except two kinds that keep the type they always had: the sign-in alert (`new_sign_in`) and the check-in answers (`check_in_request.approved` / `.rejected` / `.expired`, with `bookingId`, `code`, `requestId`, `groupId` as before) |
| `category` | `updates` |
| `targetType`, `targetId` | the item's `target`, when it has one |
| anything in the item's `data` | `sessionId`; for a booking `bookingId`, `bookingCode`, `startAt`, `salonId` |

On a push: refresh section 5, and the first page of section 1 if the center
is open. On a tap: open `target`, and call section 3 with `notificationId`.

There is no socket for live updates: the push, plus a refresh when the app
comes to the front, keeps the screen right.

## 8. Errors

| When | Status | `code` |
| --- | --- | --- |
| No token | `401` | `not_authenticated` |
| Switch off, or an id that is not theirs | `404` | `not_found` |
| Bad `category`, `unread`, `page_size`, or `before` | `422` | `validation_error` |
| A `cursor` we did not give | `422` | `validation_error`, `errors[0].code` `invalid_cursor` |
| Mark all read more than 30 a minute | `429` | `rate_limited` |

## 9. Checklist

- [ ] Empty list shows the empty state.
- [ ] Tabs send `category`; All sends nothing or `all`.
- [ ] Load more follows `next` until `null`.
- [ ] Opening an item calls `POST /notifications/{id}/read`.
- [ ] Mark all read sends `before` = newest `created_at` on screen.
- [ ] Badge from `/notifications/unread-count`, refreshed on push and on
      foreground.
- [ ] Unknown `type`: plain icon, still shown.
