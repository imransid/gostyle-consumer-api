# Booking — Nearest Available

`GET /api/v1/booking/nearest-available/<uuid>` · **Bearer token required**

The bookable starts for a visit at one salon, inside one window. With
`stylist_id`, that stylist's starts; without it, starts across everyone
qualified for the picked services.

---

## 1. Request

```
GET /booking/nearest-available/33333333-…?from=2026-09-19T10:00:00%2B04:00
    &to=2026-09-19T12:00:00%2B04:00&service_ids=6666…60,6666…61
```

| Param         | Type   | Required | Notes                                                                   |
| ------------- | ------ | -------- | ----------------------------------------------------------------------- |
| `from`        | string | yes      | Window start, inclusive. ISO 8601 **with an offset**.                   |
| `to`          | string | yes      | Window end, exclusive. Same salon-local day as `from`.                  |
| `service_ids` | string | cond.    | Comma separated, same spellings as the Expert step. Required without `stylist_id`. |
| `stylist_id`  | string | cond.    | One stylist. Required without `service_ids`.                            |

Send one or the other; both together narrow to that stylist for those
services; neither is `422 missing_filter`.

**Encode the offset.** A raw `+` decodes to a space, so `+04:00` travels as
`%2B04:00`. A caller who forgets is tolerated — the space is read back as a
`+` — but nothing else about the string is guessed: a time with no offset at
all is `422`, because "10:00" in an unnamed timezone is not a moment.

**One salon day per window.** `to` is exclusive, so 20:00 → 00:00 is one
evening and is accepted; 20:00 → 02:00 is two days and is not. The day is the
salon's: a caller in London asking about their own 22:00 is asking about
tomorrow morning in Dubai, and that is one day there.

---

## 2. Response — `200 OK`

```json
{
  "duration_min": 55,
  "offers": [
    {
      "start": "2026-09-19T12:00:00+04:00",
      "end": "2026-09-19T12:55:00+04:00",
      "bookings_today": 1,
      "stylist": {
        "id": "99999999-9999-9999-9999-999999999991",
        "name": "Darius Stone",
        "role": "Haircut and Styling Expert",
        "avatar_url": null
      }
    }
  ]
}
```

| Field              | Type           | Notes                                                                 |
| ------------------ | -------------- | --------------------------------------------------------------------- |
| `duration_min`     | number         | The whole visit in minutes, padding included. See §3.                 |
| `offers`           | array          | Bookable starts, soonest first. Empty is a valid answer.              |
| `↳ start` / `end`  | string         | ISO 8601 in **the salon's** offset, whatever offset the caller used.  |
| `↳ bookings_today` | number         | Appointments that stylist already holds on the window's day.          |
| `↳ stylist`        | object         | `id`, `name`, `role`, `avatar_url` — repeated on every offer.         |

`role` is the same field, from the same column, as the stylists endpoint
(`BOOKING_EXPERT_API.md` §2), so one stylist reads the same on every screen.

**The window bounds the start, not the end.** A 45-minute service starting at
11:45 in a 10:00–12:00 window is a real offer when the stylist is free until
12:30. **Nothing to offer is `200` with `"offers": []`** — an unknown stylist,
a stylist who cannot do the work, an id from another salon's menu, a closed
day, a full diary, a window already past.

---

## 3. How a start is decided

A minute is bookable when four things agree. Each is subtracted from the last,
and what survives is walked on the grid (`apps/salons/slots.py`).

1. **The salon is open.** The published weekly hours for that date, and not
   shut by hand in the platform. Overnight hours (18:00 → 02:00) are eight
   hours, not minus sixteen.
2. **The stylist is rostered here that day.** One `shift` row: start, end, and
   optionally the start of a fixed one-hour unpaid break. The salon's hours
   bound the shift, so a 09:00 start against a 10:00 opening is not a 09:00
   offer.
3. **The break and existing appointments are cut out.** One block can split a
   shift in two; an appointment may not straddle the gap.
4. **The start is not in the past.** Now, plus the longest `lead_time_minutes`
   of the picked services.

Then: every grid point where the whole appointment fits, in the window.

- **Grid: 15 minutes**, counted from salon-local midnight, so starts land on
  :00/:15/:30/:45 whatever time the salon opens.
- **Length** is the sum over the picked services of the platform's own stage
  arithmetic — `buffer_pre + work + duration_impact + buffer_post` — falling
  back to the catalogue duration for a service with no stages. `end` already
  includes that padding and is never recomputed downstream.
- **Qualified** is stricter than on the Expert step: one stylist for the whole
  visit, so covering part of the basket is covering none of it. The skill
  match itself is identical — see `BOOKING_EXPERT_API.md` §3.
- **Busy** means a `booking` row that is `PENDING`, `CONFIRMED`, `CHECKED_IN`
  or `COMPLETED`, not soft-deleted. The first three are the platform's own
  `ACTIVE_STATUSES`; `COMPLETED` is added here, because the safe direction for
  a customer-facing answer is to offer fewer starts than the platform would
  accept, never one it will reject.
- **Order:** `start`, then `bookings_today` (spreading work rather than
  filling one diary), then name.

---

## 4. Errors

Only a malformed request fails.

| Case                                               | Status | `code`             |
| -------------------------------------------------- | ------ | ------------------ |
| `from`/`to` missing, unparseable, or with no offset | 422    | `invalid_window`   |
| `to` not after `from`                              | 422    | `invalid_window`   |
| `from` and `to` on different salon days            | 422    | `invalid_window`   |
| Neither `service_ids` nor `stylist_id`             | 422    | `missing_filter`   |
| `service_ids`/`stylist_id` not UUIDs               | 422    | `invalid`          |
| No bearer token                                    | 401    | `not_authenticated`|
| Salon id does not exist                            | 404    | `not_found`        |
| Unknown service or stylist, closed day, full diary | 200    | — (`"offers": []`) |

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "to",
      "code": "invalid_window",
      "message": "The end of the window must come after the start."
    }
  ]
}
```

---

## 5. Known gaps

Read this section before trusting an offer.

- **Two booking stores exist.** This endpoint reads the platform's `booking`
  table. `gostyle-booking-api` keeps its own bookings *and its holds* in a
  separate database (`gostyle_booking`), and nothing here can see them, so a
  slot someone is holding mid-checkout still reads as free. Which store is
  authoritative for customer bookings is a question for the team, not
  something this service can decide; until it is settled, treat an offer as a
  strong hint and let booking creation be the arbiter.
  (Its `GET /v1/availability` could not be proxied for this: that engine runs
  on a fixture menu with ids of its own, not the platform's services.)
- **The slot grid and the browsing default are constants, not settings.**
  15 minutes and 30 minutes, in `apps/salons/slots.py`. Nothing in the
  platform schema stores either; the 15 mirrors gostyle-booking-api's ONLINE
  channel grain, the only configured answer in the stack.
- **The unpaid break is one hour** because the platform's schema says so in a
  comment rather than a column — it stores only when the break starts.
- **Lead time is per service, and usually null.** There is no salon-wide
  minimum notice anywhere, so a salon that wants one has to set it on every
  service. gostyle-booking-api's ONLINE channel uses 60 minutes; this endpoint
  imposes no floor of its own.
- **Dated opening-hours exceptions are still unreadable** (see
  `SALON_PROFILE_API.md`), so a salon open on its normal hours over a public
  holiday will offer starts it should not.
- **Only bookings and the rostered break block a stylist.** The platform has
  no general time-off, absence or calendar-block table, and
  `staff_profile.shifts` (a JSON column) is not read.
- **Rooms, chairs and stations are not modelled here.** A service stage
  declares a `resource_type` and a `capacity`; this endpoint asks only whether
  the person is free, so two stylists can be offered the same chair.
- **`daily_limit` and `parallel_allowed` are ignored.** A service capped at
  five a day will still be offered a sixth.
- **`SPLIT` and `ONCALL` shifts are read as one plain window**, because the
  schema stores only one start, one end and one break whatever the type says.
- **One stylist for the whole visit.** A split visit — a different stylist per
  service — would be a chain of appointments and a different response shape,
  not a flag on this one.
- **Counts per window** (how many starts each of several windows holds) would
  be one call per window today. If the app starts asking that way, a summary
  endpoint is the answer, not a wider response here.
