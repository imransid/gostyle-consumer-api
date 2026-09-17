# Booking — Expert step

`GET /api/v1/salon/<uuid>/stylists?service_ids=<uuid>,<uuid>`

The Expert step lists the staff who can actually perform the services the
customer picked. It is the Stylists tab endpoint
(`SALON_PROFILE_API.md` §3) with one filter added, so both screens share one
contract and one response shape.

---

## 1. Request

| Param         | Type   | Required | Notes                                                                       |
| ------------- | ------ | -------- | --------------------------------------------------------------------------- |
| `service_ids` | string | no       | Picked service ids, comma separated. Omitted or empty = the full roster.     |

No mode flag. The endpoint answers one question: who can cover at least one of
these services, and which of them does each cover.

Three spellings are accepted and normalise to the same list, because three
clients spell an array three ways:

```
?service_ids=a,b                  the customer app
?service_ids=a&service_ids=b
?service_ids[]=a&service_ids[]=b  axios 1.x default for an array
```

Duplicates collapse. Order is kept — each stylist's `service_ids` comes back
in the order the customer picked, so the app can group the list without
sorting it again. Up to 50 ids per request.

---

## 2. Response — `200 OK`

```json
{
  "stylists": [
    {
      "id": "99999999-9999-9999-9999-999999999991",
      "tenant_id": "11111111-1111-1111-1111-111111111111",
      "branch_id": "22222222-2222-2222-2222-222222222222",
      "name": "Darius Stone",
      "title": "Master Barber",
      "role": "Haircut and Styling Expert",
      "avatar_url": null,
      "rating": null,
      "review_count": null,
      "years_experience": null,
      "day_off": null,
      "service_ids": [
        "66666666-6666-6666-6666-666666666660",
        "66666666-6666-6666-6666-666666666661"
      ]
    }
  ]
}
```

`service_ids` is present **only when the filter was sent**. The unfiltered
Stylists tab response is unchanged: the tab asks a question about people, not
about a basket, and an empty `service_ids` there would read as "this person
can do nothing".

`title` and `role` are two different columns, not one string split in two:

| Field   | Source                     | Example                     |
| ------- | -------------------------- | --------------------------- |
| `title` | `staff_profile.position`   | `Master Barber`             |
| `role`  | `user_account.job_title`   | `Haircut and Styling Expert` |

Neither falls back to the other. The app renders `title · role`, and printing
the same words on both sides of the dot is worse than leaving one side empty.
Before this change `role` carried `position`; a build that reads only `role`
now shows the expertise line instead of the job title.

`rating`, `review_count`, `years_experience` and `day_off` are still
permanently null — same reasons as `SALON_PROFILE_API.md` §3.

**Order: rating descending, then name.** Every rating is null today, so it is
name order in practice, and nothing reshuffles between refreshes.

**No match is `200` with `"stylists": []`.** Nobody for one service, or nobody
at all, is a normal answer. A partial gap answers the same way: the stylists
who qualify come back, and a requested service missing from every
`service_ids` is one nobody here can take. A 404 or 422 would put an error
screen in front of a customer who only picked an unusual service.

"Any Available Expert" is a synthetic first row in the UI. The backend never
returns it.

Picking a stylist here is not booking one: whether they have a free hour is the
next question, answered by
[BOOKING_NEAREST_AVAILABLE_API.md](BOOKING_NEAREST_AVAILABLE_API.md).

---

## 3. How the match works

```
service ──service_stage.skill_id──▶ catalog_skill     (platform-wide, seeded)
stylist ──staff_skill_assignment──▶ skill             (the tenant's own catalogue)
```

The two skill catalogues were designed apart and **nothing joins them**. The
platform's own answer to this question
(`gostyle-platform/packages/nest-staff/…/service-eligibility.handler.ts`)
bridges them by `code`, trimmed and lowercased, and this endpoint mirrors it
step for step — `apps/salons/skills.py`. Two services answering the same
question differently would be worse than either answer being wrong, so when
that handler changes, change this with it.

The rules, in full:

1. **Skills are resolved server-side.** The client sends services and receives
   people. No skills endpoint, no `skill_id` in the response.
2. **Match per service, whole.** A stylist can perform a service only by
   holding *every* skill its stages require, each at or above the level asked
   for. Partial coverage is not coverage, and two people never pool skills to
   cover one service between them.
3. **Stages of the same skill fold to the highest level.** A colour service
   with a SENIOR step and a TRAINEE step needs a SENIOR colourist, not two
   people.
4. **Levels are a floor.** Stage levels are 1..5, staff levels are
   TRAINEE < JUNIOR < SENIOR < MASTER. 1→TRAINEE, 2→JUNIOR, 3→SENIOR, 4→MASTER,
   5 clamps to MASTER — the platform's mapping, invented because the two
   modules chose different scales.
5. **A skill the tenant never added is a skill nobody holds.** If a required
   `catalog_skill.code` matches no row in the tenant's `skill` table, that
   service comes back covered by no one — 200, empty, not an error.
6. **Anyone covering at least one requested service is included**, with
   exactly the covered ids in `service_ids`. Someone covering none is left out.
7. **`stylists` is a set.** A stylist eligible for three services appears once,
   with all three ids.
8. **Inactive staff are hidden** — not ACTIVE, still INVITED, or soft-deleted —
   whatever their skills.
9. **Skills are not availability.** This answers "who is qualified", not "who
   is free on Tuesday at 3". `day_off` is display text; slots belong to the
   Time step.

---

## 4. Errors

Errors are for bad input only. A request whose ids are all valid never fails,
however few stylists come back. Same envelope as `AUTH_GUIDE.md`.

```json
{
  "detail": "Please correct the highlighted fields.",
  "code": "validation_error",
  "errors": [
    {
      "field": "service_ids",
      "code": "unknown_service",
      "message": "One or more selected services are not offered by this salon."
    }
  ]
}
```

| Case                                         | Status | `code`                  |
| -------------------------------------------- | ------ | ----------------------- |
| Service id does not exist                    | 422    | `unknown_service`       |
| Service belongs to a different salon         | 422    | `unknown_service`       |
| Service is not published or not online-bookable | 422 | `unknown_service`       |
| Service has no stages, so requires no skill  | 422    | `service_without_skill` |
| `service_ids` is not a list of UUIDs         | 422    | `invalid`               |
| More than 50 service ids                     | 422    | `invalid`               |
| Salon id does not exist                      | 404    | `not_found`             |
| Nobody covers one or more services           | 200    | — (`"stylists": []`)    |
| Nobody at this salon qualifies at all        | 200    | — (`"stylists": []`)    |

`unknown_service` is measured against the same list the services tab shows: a
service the customer could never have picked here is not a service this salon
offers, whatever the reason.

`service_without_skill` is the one that looks harsh. A service with no stages
requires no skill, so *every* stylist would trivially qualify — the endpoint
would hand back the whole roster and the customer would book someone who
cannot do the job. Surfacing the catalogue gap is the lesser evil.

---

## 5. Known gaps

- **`service_stage` is empty in every environment seeded so far.** Until the
  platform attaches stages (and their skills) to services, every filtered
  request answers `422 service_without_skill` and the Expert step has nothing
  to show. This is a catalogue-data task on the platform side, not a change
  here.
- **`skill.min_bookable_level` is ignored.** The staff module documents it as
  gating bookability, the platform's eligibility handler does not read it, and
  this mirrors the handler. If the product means it to bind, both places
  change together.
- **`service_stage_default` is not read.** It supplies defaults when a *new*
  stage is created; a service with no stages has no requirements regardless of
  what its defaults say.
- **Re-validation on booking creation is not implemented here.** Booking
  creation lives in gostyle-booking-api, and this filter is convenience, not
  security: a stylist who does not hold the service's skills must still be
  rejected there with `422 / stylist_missing_skill`.
- **`rating`, `review_count`, `years_experience`, `day_off`** — still null, see
  `SALON_PROFILE_API.md` §3.
