# Notifications and Settings: FE Integration Guide

Date: 2026-10-31. For: the mobile app team (and the coding agent that builds
the screens). One guide for the whole feature:

- the **Notification Center** (tabs All, Orders, Updates, Payments,
  Promotions) and the bell's unread badge,
- the **push** a phone gets, and what a tap opens,
- the **Notification Preferences** screen (the six switches, the channels
  Push / Email / SMS, and "Appointment reminder timing"),
- the **Privacy & Visibility** screen.

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>` on every call.

**Status: everything on this page is LIVE on production.** The older
per-screen guides (`NOTIFICATION_CENTER_API.md`,
`NOTIFICATION_PREFERENCES_API.md`, `PRIVACY_SETTINGS_API.md`) say the same
thing in more pieces; this one is the one to build from.

---

## 0. Read this first

1. **The server writes the words.** Every item comes with its `title` and
   `body`, already written with the salon's name and the time on the
   salon's clock. Show them as they are. Do not build sentences on the
   phone.
2. **Pick the icon from `type`.** A `type` you do not know yet: plain icon,
   still show it. New kinds will come; the item shape never changes.
3. **Sort and group by `created_at`**, in the phone's own time zone (Today,
   Yesterday, This week, Earlier). The server does not know the phone's
   zone.
4. **Paging is by cursor.** Follow `next` exactly as it is, until it is
   `null`. There is no page number.
5. **Opening an item does not mark it read.** Call `POST
   /notifications/{id}/read` when the customer opens it.
6. **No socket.** The push, plus a refresh when the app comes to the front,
   keeps the list and the badge right (§5).
7. **The switches decide the alert, not the history.** "Booking
   confirmation" off means no push and no email for it; the booking is
   still in the center. Two exceptions are in §6.3.
8. **A field missing from `GET /notifications/preferences` means its server
   switch is off: hide that row.** All are on today, but build it this way.
9. **Error codes.** Validation errors are `422` everywhere in this API,
   except `/notifications/preferences`, which keeps its old `400`. The
   envelope is the same everywhere (§8).
10. **Nothing is seeded.** A new customer sees an empty list (`200`,
    `"results": []`) and an unread count of `0`. Show your empty state.

## 1. Screens and endpoints at a glance

| Screen / part | Method and path | Section |
| --- | --- | --- |
| Notification Center, a tab | `GET /notifications?category=...` | 3.1 |
| Open one item | `GET /notifications/{id}` | 3.2 |
| Mark one read | `POST /notifications/{id}/read` | 3.3 |
| Mark all read | `POST /notifications/read-all` | 3.4 |
| Bell badge, tab badges | `GET /notifications/unread-count` | 3.5 |
| Push: register the phone (unchanged) | `POST /me/device-tokens` | 5.1 |
| Notification Preferences | `GET` / `PATCH /notifications/preferences` | 6 |
| Privacy & Visibility | `GET` / `PATCH /me/privacy` | 7 |

## 2. TypeScript types (copy these)

```ts
// ---------- Notification Center ----------
type Category = 'orders' | 'updates' | 'payments' | 'promotions';
type Tab = 'all' | Category;

type NotificationType =
  | 'booking.confirmed'
  | 'booking.rescheduled'
  | 'booking.cancelled'
  | 'booking.reminder'
  | 'check_in.approved'
  | 'check_in.rejected'
  | 'check_in.expired'
  | 'account.new_sign_in'
  // Known now, nothing sends them yet (§9):
  | 'payment.completed'
  | 'payment.refunded'
  | 'promo.offer'
  | 'promo.new_salon'
  | 'promo.loyalty'
  | 'salon.new_styles'
  | (string & {}); // new kinds will come: never crash on one you do not know

interface NotificationItem {
  id: string;                 // uuid
  category: Category;         // its tab
  type: NotificationType;
  title: string;              // show as it is
  body: string;               // show as it is
  is_read: boolean;
  read_at: string | null;     // UTC ISO
  created_at: string;         // UTC ISO. Sort and group by this.
  occurred_at: string;        // UTC ISO. When the thing happened.
  target: { type: string; id: string } | null; // where a tap goes (§4.2)
  data: Record<string, string>; // all strings, keys by type (§4.1)
}

interface NotificationPage {
  next: string | null;        // full URL; call it as it is
  previous: string | null;
  results: NotificationItem[];
}

interface UnreadCount {
  total: number;
  orders: number;
  updates: number;
  payments: number;
  promotions: number;
}

interface ReadAllBody {
  category?: Tab;             // default 'all'
  before?: string;            // created_at of the newest item on screen
}

interface ReadAllResult {
  updated: number;            // how many were marked now
  unread: UnreadCount;        // redraw the badges from this
}

// ---------- Push (the FCM message's data; every value is a string) ----------
interface PushData {
  type: string;               // §5.2: two kinds keep their older type
  notificationId?: string;    // the item's id
  category?: Category;
  targetType?: string;
  targetId?: string;
  // by type, as in the item's data (§4.1):
  bookingId?: string;
  bookingCode?: string;
  startAt?: string;
  salonId?: string;
  seriesId?: string;
  groupId?: string;
  code?: string;
  requestId?: string;
  sessionId?: string;
}

// ---------- Notification Preferences ----------
type ReminderLead = 30 | 60 | 120 | 1440; // minutes; 1440 = 1 day

interface NotificationPreferences {
  booking_confirmation: boolean;
  appointment_reminder: boolean;
  booking_changes: boolean;
  special_offers: boolean;
  new_salons: boolean;
  loyalty: boolean;
  push: boolean;
  email?: boolean;                    // missing: hide the row (rule 8)
  sms?: boolean;
  reminder_lead_minutes?: ReminderLead;
  channels_available?: { push: boolean; email: boolean; sms: boolean }; // read-only
}

type NotificationPreferencesPatch = Partial<
  Omit<NotificationPreferences, 'channels_available'>
>;

// ---------- Privacy & Visibility ----------
interface PrivacySettings {
  show_name_to_salons: boolean;
  show_photo_in_moments: boolean;
}

// ---------- Errors (every route) ----------
interface ApiError {
  detail: string;             // a sentence; safe to show
  code: string;               // 'validation_error', 'not_found', ...
  errors: { field: string | null; code: string; message: string }[];
}
```

---

## 3. Notification Center

### 3.1 The list: `GET /notifications`

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

Newest first. Load more by calling `next` as it is, until it is `null`. An
item that arrives while the customer scrolls does not show twice.

`200`:

```json
{
  "next": "https://api.gostyle.uk/api/v1/notifications?cursor=cD0yMDI2LTEwLTMxKzA4JTNBMjA%3D&page_size=20",
  "previous": null,
  "results": [
    {
      "id": "0c9e4f7a-5d2b-4a61-8f3e-2b7d9c1e6a40",
      "category": "orders",
      "type": "booking.confirmed",
      "title": "Booking confirmed",
      "body": "Your booking GS-1485 at Demo Salon & Spa on Sun 1 Nov at 12:00 PM is confirmed.",
      "is_read": false,
      "read_at": null,
      "created_at": "2026-10-31T13:01:02.114000Z",
      "occurred_at": "2026-10-31T13:01:01.870000Z",
      "target": {"type": "booking", "id": "5b8e2f10-7c4a-4d39-9e61-0a3f2c8d7b15"},
      "data": {
        "bookingId": "5b8e2f10-7c4a-4d39-9e61-0a3f2c8d7b15",
        "bookingCode": "GS-1485",
        "startAt": "2026-11-01T08:00:00.000Z",
        "salonId": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5"
      }
    }
  ]
}
```

Items are kept for 180 days.

### 3.2 One item: `GET /notifications/{id}`

`200` with one item, the same shape. **It does not mark it read** (§3.3).
`404` for an id that is not theirs, unknown, or not an id.

### 3.3 Mark one read: `POST /notifications/{id}/read`

No body. `200` with the item, `is_read: true`. Safe to repeat: the first
`read_at` stays. Call it when the customer opens an item, from the list or
from a push.

### 3.4 Mark all read: `POST /notifications/read-all`

```http
POST /notifications/read-all
Content-Type: application/json

{"category": "all", "before": "2026-10-31T13:01:02.114000Z"}
```

Both fields are optional.

- `category`: one tab, or `all` (default).
- `before`: send the `created_at` of the **newest item on screen**. Only
  items created at or before it are marked, so one that arrived while the
  customer tapped stays unread.

`200`:

```json
{"updated": 4, "unread": {"total": 0, "orders": 0, "updates": 0, "payments": 0, "promotions": 0}}
```

Redraw the badges from `unread`; no second call needed. More than 30 calls
a minute answers `429`.

### 3.5 Unread count: `GET /notifications/unread-count`

```json
{"total": 3, "orders": 1, "updates": 2, "payments": 0, "promotions": 0}
```

Cheap. `total` is the bell badge; the others are the tab badges. Call it
when the app comes to the front, when a push arrives, and after a read.

---

## 4. The items

### 4.1 Every type

| `type` | Tab | Title (example body) | `target.type` | `data` keys |
| --- | --- | --- | --- | --- |
| `booking.confirmed` | orders | "Booking confirmed" ("Your booking GS-1485 at Demo Salon & Spa on Sun 1 Nov at 12:00 PM is confirmed.") | `booking` | `bookingId`, `bookingCode`, `startAt`, `salonId` |
| `booking.confirmed`, a routine | orders | "Routine confirmed" ("Your routine at Demo Salon & Spa is confirmed: 4 visits, starting Mon 2 Nov at 11:00 AM.") | `booking` | the same, plus `seriesId` |
| `booking.confirmed`, a party | orders | "Booking confirmed" ("Your group booking at Demo Salon & Spa on Mon 2 Nov at 11:00 AM is confirmed.") | `booking` | the same, plus `groupId` |
| `booking.rescheduled` | orders | "Booking moved" ("Your booking GS-1483 at Demo Salon & Spa has a new time: Tue 3 Nov at 2:00 PM.") | `booking` | `bookingId`, `bookingCode`, `startAt`, `salonId` |
| `booking.cancelled` | orders | "Booking cancelled" ("Your booking GS-1485 at Demo Salon & Spa on Sun 1 Nov at 12:00 PM was cancelled.") | `booking` | the same |
| `booking.reminder` | updates | "Appointment reminder" ("Your booking GS-1484 at Demo Salon & Spa is tomorrow at 3:45 PM.") | `booking` | the same |
| `check_in.approved` | orders | "You are checked in" ("Welcome! Booking GS-1490.") or, for a party member, "Sara A. is checked in" ("Booking GS-1490.") | `booking` | the same, plus `code`, `requestId`, and `groupId` for a party |
| `check_in.rejected` | orders | "Please speak to the desk" ("The salon could not confirm your arrival. Booking GS-1490.") | `booking` | as approved |
| `check_in.expired` | orders | "Please speak to the desk" ("The salon did not answer your check-in in time. Booking GS-1490.") | `booking` | as approved |
| `account.new_sign_in` | updates | "New sign-in to your account" ("Your Go Style account was just signed in to on iPhone 15 in Dubai, UAE. If this was not you, change your password.") | `session` | `sessionId` |

Notes:

- **One "confirmed" per routine and per party**, not one per visit or per
  member. A routine of 4 visits is one item; a party of 3 is one item for
  each account in it.
- A booking paid in the app is "confirmed" when its payment is recorded,
  not when it is first created. A booking still waiting for its payment
  has no item yet.
- A key with no value is left out of `data` (for example `salonId` when the
  salon could not be found). Read every key as optional.
- `startAt` is UTC ISO. The `body` already has the time on the salon's
  clock; use `startAt` only for logic, never to rewrite the words.

### 4.2 What a tap opens

| The item has | Open |
| --- | --- |
| `data.seriesId` | the routine: `GET /booking/{seriesId}` (routine guide §10) |
| `data.groupId` | the party: `GET /booking/{groupId}` (group guides) |
| `target.type` `booking` | the booking: `GET /booking/{target.id}` |
| `target.type` `session` | Settings, "Where you're signed in" (the device sessions screen) |
| `target` `null`, or a `target.type` you do not know | nothing; just mark it read |

Then call `POST /notifications/{id}/read`. A booking that is gone answers
`404` on its read: show "This booking is no longer available" and keep the
item.

---

## 5. Push

### 5.1 Register the phone (unchanged)

As today: after sign-in, and whenever FCM gives a new token:

```http
POST /me/device-tokens
Content-Type: application/json

{"token": "<fcm token>", "platform": "ios"}
```

`204`. On sign-out, `DELETE /me/device-tokens` with the same body. Nothing
new here.

### 5.2 What a push carries

Every push from the center has a visible part (the item's `title` and
`body`) and a `data` part, all strings:

| Key | Value |
| --- | --- |
| `notificationId` | the item's `id` |
| `type` | the item's `type`, **except two kinds that keep the type they had before the center**, so app builds already in customers' hands keep working: the sign-in alert sends `new_sign_in`, and the check-in answers send `check_in_request.approved`, `check_in_request.rejected`, `check_in_request.expired` |
| `category` | the item's tab |
| `targetType`, `targetId` | the item's `target`, when it has one |
| every key of the item's `data` | as in §4.1 |

Example, a confirmed booking:

```json
{
  "notificationId": "0c9e4f7a-5d2b-4a61-8f3e-2b7d9c1e6a40",
  "type": "booking.confirmed",
  "category": "orders",
  "targetType": "booking",
  "targetId": "5b8e2f10-7c4a-4d39-9e61-0a3f2c8d7b15",
  "bookingId": "5b8e2f10-7c4a-4d39-9e61-0a3f2c8d7b15",
  "bookingCode": "GS-1485",
  "startAt": "2026-11-01T08:00:00.000Z",
  "salonId": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5"
}
```

Map the push `type` back to the item type like this:

```ts
const PUSH_TYPE_TO_ITEM_TYPE: Record<string, string> = {
  new_sign_in: 'account.new_sign_in',
  'check_in_request.approved': 'check_in.approved',
  'check_in_request.rejected': 'check_in.rejected',
  'check_in_request.expired': 'check_in.expired',
};
const itemType = PUSH_TYPE_TO_ITEM_TYPE[data.type] ?? data.type;
```

### 5.3 What the app does

| When | Do |
| --- | --- |
| A push arrives, app in front | Refresh the badge (§3.5). If the center is open, reload its first page. A check-in answer: also refresh the booking or party screen if it is open (check-in guides). |
| The customer taps a push | Open the screen from §4.2 (use `targetType` / `targetId`, `seriesId`, `groupId`). Then `POST /notifications/{notificationId}/read`. |
| The app comes to the front | Refresh the badge. If the center is open, reload its first page. |
| A push with no `notificationId` | An older kind of push (not from the center). Handle it as you do today. |

**Draw from the reads, never from the push.** The push says something
changed; the booking screen's own `GET` says what it is now.

Each event is sent once to each of the customer's signed-in phones. A
repeat of the same event (a retry on our side) is dropped before it
reaches FCM.

---

## 6. Notification Preferences

### 6.1 `GET /notifications/preferences`

```json
{
  "booking_confirmation": true,
  "appointment_reminder": true,
  "booking_changes": true,
  "special_offers": false,
  "new_salons": false,
  "loyalty": true,
  "push": true,
  "email": true,
  "sms": false,
  "reminder_lead_minutes": 1440,
  "channels_available": {"push": true, "email": true, "sms": false}
}
```

| Row on the screen | Field | Default |
| --- | --- | --- |
| Booking confirmation | `booking_confirmation` | `true` |
| Appointment reminder | `appointment_reminder` | `true` |
| Booking changes | `booking_changes` | `true` |
| Special offers | `special_offers` | `false` |
| New salons | `new_salons` | `false` |
| Loyalty rewards | `loyalty` | `true` |
| Channel: Push | `push` | `true` |
| Channel: Email | `email` | `true` |
| Channel: SMS | `sms` | `false` |
| Appointment reminder timing | `reminder_lead_minutes`: `30`, `60`, `120`, `1440` (1 day) | `1440` |
| (no row) | `channels_available`: read-only, can this channel reach the customer at all? | |

Show each switch's own value. Grey a channel out when its
`channels_available` entry is `false`:

- `email` is `false` without a **verified** email address. `email: true`
  with `channels_available.email: false` is normal: the switch is on,
  there is just no verified address yet. Offer "Verify your email".
- `sms` is always `false` today: there is no SMS provider. Grey the SMS row
  out, or hide it.

### 6.2 `PATCH /notifications/preferences`

Partial: send only what changed. All of it is saved, or none.

```http
PATCH /notifications/preferences
Content-Type: application/json

{"email": false, "reminder_lead_minutes": 60}
```

`200` with the whole object, as in §6.1.

- Switches are JSON `true` / `false` only. Not `"true"`, not `1`.
- `reminder_lead_minutes` is a JSON number: `30`, `60`, `120` or `1440`.
  Not `"60"`, not `45`.
- `channels_available` sent back is ignored.
- Turning `email` or `sms` **off** always works. Sending a value it already
  has always works too.
- Save each switch as it is flipped (one `PATCH` per tap is fine). On an
  error, put the switch back and show the message.

### 6.3 What each switch does

| Switch off | What stops | Still in the center? |
| --- | --- | --- |
| Booking confirmation | the push and email for "confirmed" | yes |
| Booking changes | the push and email for "moved" and "cancelled" | yes |
| Appointment reminder | **the reminder itself**: nothing is sent and nothing is added | **no** (exception 1) |
| Special offers, New salons, Loyalty | the offer itself | **no** (exception 2: an offer the customer said no to is not kept) |
| Push (channel) | every push, of every kind | yes |
| Email (channel) | every email from the center | yes |

Never silenced by the six switches (only by the Push channel): the
check-in answers (they answer the customer's own tap) and the new sign-in
alert (account safety).

Which kinds can email: confirmed, moved, cancelled, and the reminder. The
check-in answers and the sign-in push are push only (the sign-in alert's
email is its own setting, `/me/security`).

### 6.4 Reminder timing: the rules

- **One reminder per visit, at the chosen time** before the start. A new
  choice counts for every coming visit within a minute.
- It covers bookings **made in the app**. A booking the salon made at its
  desk is not covered by this setting.
- A booking made (or moved) after its reminder time gets no reminder: its
  "confirmed" (or "moved") just told them.
- The customer picks an earlier time after it has already passed, or turns
  reminders back on late: the reminder goes at once.
- Nothing is sent in the last 15 minutes before the visit.
- A visit that moves gets a new reminder for its new time; a cancelled one
  gets none.

### 6.5 Errors (`400` on this route)

```json
{
  "detail": "Invalid notification preference.",
  "code": "validation_error",
  "errors": [
    {"field": "email", "code": "email_not_verified", "message": "Verify your email address first."}
  ]
}
```

| `field` | `code` | When | The app |
| --- | --- | --- | --- |
| `email` | `no_email_on_account` | turning email on, no email on the account | "Add an email address" |
| `email` | `email_not_verified` | turning email on, the email is not verified | "Verify your email", then try again |
| `sms` | `no_phone_on_account` | turning SMS on, no phone | "Add a phone number" |
| `sms` | `phone_not_verified` | turning SMS on, the phone is not verified | "Verify your phone" |
| `sms` | `sms_unavailable` | turning SMS on: no provider yet | keep it off, show `message` |
| `reminder_lead_minutes` | `invalid` | not one of the four numbers | a bug: send a number |
| any switch | `invalid` | not a JSON boolean | a bug: send `true` / `false` |
| a key we do not know | `unknown_field` | a typo, or a field whose server switch is off | a bug, or hide that row (rule 8) |

---

## 7. Privacy & Visibility

| Row on the screen | Where it lives |
| --- | --- |
| Show my name to salons | `show_name_to_salons` (this API) |
| Show my photo in Moments | `show_photo_in_moments` (this API) |
| Download my data | not built yet (§9): hide the row |
| Location access | the phone's own permission. The server keeps nothing for it. Open the phone's settings. |

### 7.1 `GET /me/privacy`

```json
{"show_name_to_salons": true, "show_photo_in_moments": false}
```

### 7.2 `PATCH /me/privacy`

Partial, all or nothing, real JSON booleans only.

```http
PATCH /me/privacy
Content-Type: application/json

{"show_name_to_salons": false}
```

`200` with both settings.

- **Show my name to salons, off** (default on): salons see the first name
  and the initial of the last name, "Sara A.", enough for the desk to call
  them. It applies at once to the salon's record of them that the app
  made, the name at the check-in desk, and their name on a party lane. A
  salon's own record from a visit before the app stays as the salon wrote
  it. Say so under the switch, for example: "Salons that knew you before
  GoStyle keep their own records."
- **Show my photo in Moments** (default off) is kept for Moments, which is
  not built yet. It changes nothing today.

| When | Status | `code` (`errors[0].code`) |
| --- | --- | --- |
| Not a JSON `true` / `false` | `422` | `validation_error` (`invalid`) |
| A key we do not know | `422` | `validation_error` (`unknown_field`) |
| The salon's record could not be updated: nothing was saved, try again | `503` | `service_unavailable` |

---

## 8. Errors on every route

The envelope (every route in this API):

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [{"field": "category", "code": "invalid_choice", "message": "\"inbox\" is not a valid choice."}]
}
```

| When | Status | `code` | The app |
| --- | --- | --- | --- |
| No token, or it expired | `401` | `not_authenticated` | refresh the token, as everywhere |
| An item that is not theirs, or not an id | `404` | `not_found` | "Not found"; reload the list |
| Bad `category`, `unread`, `page_size` or `before` | `422` | `validation_error` | a bug |
| A `cursor` we did not give | `422` | `validation_error`, `errors[0].code` `invalid_cursor` | start again from the first page |
| Mark all read, more than 30 a minute | `429` | `rate_limited` | wait, then try again |
| Preferences, a bad field | `400` | `validation_error` | §6.5 |
| Privacy, the salon's record failed | `503` | `service_unavailable` | "Please try again" |

---

## 9. Not there yet (build the UI, expect it empty)

| Part | Today | Build now? |
| --- | --- | --- |
| **Payments** tab | empty: no real payment gateway yet. The types are known (`payment.completed`, `payment.refunded`). | Yes: the tab, with its empty state. |
| **Promotions** tab | empty: nothing sends offers yet. The types are known (`promo.offer`, `promo.new_salon`, `promo.loyalty`). | Yes: the tab, with its empty state. |
| **SMS** channel | stored, never sent (`sms_unavailable`) | Grey the row out (`channels_available.sms` is `false`). |
| **Download my data** | not built | Hide the row. |
| **Show my photo in Moments** | saved, no effect (no Moments yet) | Yes, the switch. |

When one of these goes live, nothing changes on the app's side: the same
item shape, the same routes, the tab simply fills.

---

## 10. How to see each kind on a test account

| To see | Do |
| --- | --- |
| `account.new_sign_in` | Sign in on a second phone (or after signing out). |
| `booking.confirmed` | Book a visit in the app and pay (or choose pay after check-in). One item, once the payment is recorded. |
| "Routine confirmed" | Book a routine of 2 or more visits. **One** item. |
| A party's "confirmed" | Book a group. **One** item for each account in it. |
| `booking.rescheduled` | Move a booking. One item per move. |
| `booking.cancelled` | Cancel a booking. |
| `booking.reminder` | Set "Appointment reminder timing" to 2 hours, then book a visit about 2.5 hours from now. The reminder comes 2 hours before the start (about 30 minutes later). A visit booked closer than its reminder time gets none (§6.4). |
| `check_in.approved` / `rejected` | Tap "I am here" on a visit (from 30 minutes before its start); the desk approves or rejects on the business web. |
| `check_in.expired` | Tap "I am here" and let nobody answer until the visit's end time. |
| No push, still in the center | Turn "Booking confirmation" off, then book. |
| No push at all | Turn the Push channel off, then cancel a booking. The item is there; no push. |

Already proven on production: new sign-in, confirmed, moved, cancelled,
reminder (with push and email). Live but not yet seen on a real booking:
the check-in answers in the center, and the one-per-routine / one-per-party
"confirmed". Tell the backend team if either looks wrong.

---

## 11. QA checklist

Notification Center:

- [ ] Empty list shows the empty state; badge `0`.
- [ ] Tabs send `category`; All sends nothing or `all`.
- [ ] Load more follows `next` until `null`; no item shows twice.
- [ ] Items grouped Today / Yesterday / This week / Earlier by
      `created_at` in the phone's zone.
- [ ] `title` and `body` shown as they are.
- [ ] An unknown `type` shows with a plain icon.
- [ ] Opening an item calls `POST /notifications/{id}/read`; its dot goes.
- [ ] Mark all read sends `before` = newest `created_at` on screen, and
      redraws the badges from `unread`.
- [ ] Badge from `/notifications/unread-count`, refreshed on push, on
      foreground, and after a read.
- [ ] A tap on a routine's item opens the routine; a party's opens the
      party; a sign-in alert opens "Where you're signed in".

Push:

- [ ] `new_sign_in` and `check_in_request.*` are mapped to their item types.
- [ ] A tap opens the right screen and marks the item read with
      `notificationId`.
- [ ] A push without `notificationId` still works as before.

Preferences:

- [ ] All rows read their own field; a missing field hides its row.
- [ ] Email greyed while `channels_available.email` is `false`, with
      "Verify your email".
- [ ] SMS greyed (`channels_available.sms` is `false`).
- [ ] Reminder timing sends `reminder_lead_minutes` as a number.
- [ ] A refused switch flips back and shows `errors[0].message`.

Privacy:

- [ ] Both switches read and save; a `503` flips the switch back with
      "Please try again".
- [ ] "Download my data" hidden; "Location access" opens the phone's
      settings.
