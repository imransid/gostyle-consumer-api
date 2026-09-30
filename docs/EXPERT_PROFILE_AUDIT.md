# Expert Profile screen: the app team's contract vs our code (audit)

Date: 2026-09-30. **Audit only: no code was changed.**

The contract: the app team's "Expert Profile Screen" (one new route,
`GET /salon/{salon_id}/stylist/{stylist_id}`, and `stylist_id` on
`POST /favourite`), received 2026-09-30 and saved as received in
`docs/expert-profile-fe-contract.md` (never edit it). Section numbers below
(§2, §3, ...) are theirs.

**Status: Q1 to Q19 answered by Rafa (2026-09-30). The contract text was read
after the answers and raises nothing new for Rafa** (3.6 lists what differs
from their text; every item follows a decision already made). Live checks
(Rafa, 2026-09-30, section 9): no business has two salons and no stylist has a
wrong, missing or deleted home, so E0 changes nothing live and gets **no
switch**; rosters are filled, so `day_off` is a **final yes**.

Code read:

- **customer-api**: branch `feat/expert-profile`, made from `imransid/main` =
  `95202e1`. Test baseline on this branch: 840 tests, the same 18 known
  failures as `main`.
- **booking-api**: `main` = `cb84fe7`. Read only. Live switches (Rafa):
  `STAFF_FROM_PLATFORM=true`, `SKILLS_UNVERIFIED=true`.
- **gostyle-platform**: `ffd6406e` (the same tree as its `origin/main`
  `aa8ea613`). Read only.
- The local Postgres for real column types, enum values and indexes.

Tags: **READY** exists, same meaning. **SHAPE** the data exists, needs mapping
or a choice. **MISSING** no data anywhere today.

---

## 0. Short version

1. **One new read route and one small write change, all in customer-api.**
   `GET /salon/{salon_id}/stylist/{stylist_id}` is new. `POST /favourite`
   learns `stylist_id`, which needs a new table in our own schema (one
   migration, additive). Nothing in booking-api or the platform has to change
   for a first version.
2. **Field tags (19 fields):** READY 6 (`id`, `salon_id`, `name`, `title`,
   `role`, `avatar_url`). SHAPE 6 (`day_off`, `has_story`, `media`,
   `media_count`, `salon`, `service_groups`). MISSING 7 (`bio`, `rating`,
   `review_count`, `years_experience`, `price_level`, `is_network_member`,
   `is_favorite`). The contract lets every MISSING one be empty, except
   `is_network_member` (we send `false` and ask what it means) and
   `is_favorite` (ours to build).
3. **The person is READY**, from the same code as the Expert step, so the same
   person reads the same on both screens.
4. **Five things do not exist on the platform:** bio, years of experience, a
   price level for a stylist, a day off, and "network member". The platform's
   own staff gRPC says so in a comment and sends them empty. Decided: `bio`,
   `years_experience` and `price_level` are `null`, `is_network_member` is
   `false`, and `day_off` is worked out from the roster when it is steady (Q7).
5. **The stylist's own photos: a column exists, nothing uses it.**
   `storefront_media.staff_id`. It is never checked on write and no platform
   screen reads it. The platform accepts only images there, so every item is
   an image today (F12).
6. **Stories belong to the salon, not to a stylist.** Decided: `has_story`
   means "the stylist has media" (Q10), as their own open point says.
7. **A stylist belongs to ONE branch in the platform, but our list shows the
   whole business.** booking-api accepts only the stylists whose home branch
   is the salon's branch, and their §3 says a stylist of another salon is 404.
   The fix (E0) goes into the shared list, so the Expert step, `experts`,
   nearest available and this screen all agree. Nothing changes live today
   (no business has two salons). See 3.1, F1.
8. **`GET /booking/nearest-available/{salon_id}?stylist_id=` works as their §4
   expects**, with limits: it needs a token, it answers one day per call, and
   an unknown stylist is `200` with `offers: []`, not 404. See 3.2.
9. **`POST /favourite` has a bug today:** a `salon_id` that is not a UUID
   answers **500**. Fixed in E3a. See F3.

---

## 1. Field by field

From the contract's own field table (§2). Platform tables are read through
`apps/platform_data/models.py` (inspectdb, `managed = False`). "Expert step" =
`GET /salon/:id/stylists` (`stylist_rows`, `_stylist_row`,
`views.py:406-465`). The last column is what we send (section 7).

| # | Field | Their type | Where it comes from today | Helper that already reads it | Tag | We send |
|---|---|---|---|---|---|---|
| 1 | `id` | string | `staff_profile.id` | `salon_stylists`, `_stylist_row` | READY | The id the booking flow sends in `stylists`. |
| 2 | `salon_id` | string | the path id = `storefront.id` | `salon_profile` | READY | Echo. |
| 3 | `name` | string | `user_account.first_name` + `last_name` (joined by `staff_profile.user_id`) | `_stylist_row` | READY | `null` when both are empty (their type says string). |
| 4 | `title` | string, can be null | `staff_profile.position` | `_stylist_row` | READY | Nullable, as they allow. |
| 5 | `role` | string, can be null | `user_account.job_title` | `_stylist_row` | READY | Nullable. Never falls back to `title`. |
| 6 | `bio` | string, null hides | no column | none | MISSING | `null` (Q4). Not on `staff_profile`, not on `user_account`. |
| 7 | `avatar_url` | string, null = initials | `user_account.avatar_file_item_id`, then `file_item.url` | `salon_stylists` (annotation) | READY | Plain S3 URL. The platform fills it at staff onboarding since its commit `d4bae0cf` (2026-09-28). |
| 8 | `rating` | number, null when unrated | nothing per stylist | none | MISSING | `null` (Q14). `storefront_review` has no staff column. |
| 9 | `review_count` | number | nothing per stylist | none | MISSING | `0` (Q14): "No reviews yet". The Expert step row says `null` for the same key. |
| 10 | `years_experience` | number, null allowed | no column | none (key exists in the list, always `null`) | MISSING | `null` (Q5). |
| 11 | `price_level` | number 1 to 4, null hides | nothing per stylist | none | MISSING | `null` (Q6). |
| 12 | `day_off` | string, full day name, null hides | no column, no time off table. Dated `shift` rows in a weekly `shift_roster` per branch, and the salon's published weekly hours | `shift_rows` (one day only); key exists in the list, always `null` | SHAPE | Worked out when steady, among the days the salon is open: "Tuesday", "Friday, Saturday". Else `null` (Q7). |
| 13 | `is_network_member` | boolean | nothing | none | MISSING | `false` (Q8). Our FE doc asks what it should mean. |
| 14 | `is_favorite` | boolean | nothing for a stylist | `with_is_favorite` (salons only) | MISSING | The real value from a new table of ours; `false` for a guest (Q1, Q11). |
| 15 | `has_story` | boolean | `storefront_story` is per salon, no stylist column | `salon_stories` (salon only) | SHAPE | `true` when the stylist has media (Q10). |
| 16 | `media` | array, at most 5, newest first | `storefront_media` rows with `staff_id` = the stylist | none | SHAPE | Q9. Items below. |
| | ↳ `id` | (in their example) | `storefront_media.id` | | | The photo's own id. |
| | ↳ `type` | `image` or `video` | `storefront_media.mime_type` | | | `image` for every row today (F12). |
| | ↳ `url` | string | `storefront_media.url` | | | Plain S3 URL, as the salon gallery. |
| | ↳ `thumbnail_url` | null for an image, required for a video | nothing for a video | | | `null` for an image. A video has no thumbnail source, so it is left out (Q9). |
| 17 | `media_count` | number | count of the same rows | none | SHAPE | Counts every row that passes the same rules (Q9). |
| 18 | `salon` | object | `storefront` + `branch` + published snapshot (`IDENTITY`, `HOURS`, `MAP`) + manual state | `salon_profile`, `read_snapshot`, `hours.resolve`, `SalonProfileSerializer` | SHAPE | Q16. Keys below. |
| | ↳ `id`, `name` | (in their example) | the storefront id; published name, else branch name | | | As `GET /salon/{id}`. |
| | ↳ `is_open` | boolean | `hours.resolve` | | | `true`, `false`, or `null` when the salon published no hours. |
| | ↳ `hours_today` | string, null hides | `hours.resolve` | | | "10:00 AM - 9:00 PM"; `null` when closed today. |
| | ↳ `latitude`, `longitude` | number | published `MAP` pin, else the branch pin | | | `null` when the salon has no pin. |
| 19 | `service_groups` | array | the salon menu (`service`) filtered by the stylist's skills | `salon_services`, `stylist_service_coverage`, the tab's grouping, `menu.service_price` | SHAPE | Groups as `{id, name, services}` (Q15). |
| | ↳ `services[]` | the tab's fields | the tab's `_service` | | | `id`, `name`, `description`, `price`, `duration_min`, `duration_max`. |

---

## 2. Reuse notes

### 2.1 The person: `salon_stylists` and `stylist_rows`

`salon_stylists(salon)` (`selectors.py:253`) is the one list of "who works
here". Its rules today: `staff_profile` of the salon's **tenant**,
`employment_status = ACTIVE`, `onboarding_state = ACTIVE`, not deleted.

Everything that reads it today (so everything E0 changes):

| Caller | What it does with the list |
|---|---|
| `GET /salon/:id/stylists` (`stylist_rows`) | the Stylists tab and the Expert step |
| `GET /stylists?tenant_id=&branch_id=` (`StylistListView`) | the same rows, see F13 |
| `GET /salon/:id/service/:id` | `experts` |
| `GET /booking/nearest-available/:id` (`_qualified_stylists`) | whose times to offer |
| group booking (`group_views._roster`) | checks the stylists the app chose |
| routine read (`routine_views`) | avatars by stylist id |
| `booking_readiness` command | counts |

For this screen: `salon_stylists(salon).filter(id=stylist_id).first()`. One
query. `name`, `title`, `role` and `avatar_url` are built by the same code as
the Expert step row, so they are the same by construction.

Two differences from the Expert step row, both decided:

- `review_count` is `0` here (Q14) and `null` in the list.
- The profile sends only the contract's keys. The list row also has
  `tenant_id` and `branch_id`.

`day_off` is filled by one helper for both (Q7), so the list and the profile
never disagree.

### 2.2 "Only what this stylist does": `stylist_service_coverage`

`stylist_service_coverage(salon, service_ids, staff_ids, stages)` answers
"which of these services can this person do alone". For the profile:

```
services = salon_services(salon)              # the tab's own list, branch price read
stages   = service_stage_rows([all ids])      # one query
covered  = stylist_service_coverage(salon, [all ids], [stylist.id], stages=stages)
```

Then group the covered services exactly like the Services tab. Because it is
the same code, three answers agree by construction: a service is in this
stylist's `service_groups` exactly when the stylist is in
`GET /salon/:id/stylists?service_ids={that service}` and in the service
detail's `experts`. That is their §3 rule 4 ("the same filter, in reverse").

Notes:

- **A service with no stages is in nobody's list.** The stylists route answers
  422 `service_without_skill` for it; here it is simply left out. (The
  platform's own rule says "no stages = everyone can do it". Rafa chose to keep
  ours, 2026-09-30.)
- **The grouping is not reusable yet.** The loop that builds `service_groups`
  and the chips is inline in `SalonServicesView.get` (`views.py:356-387`). It
  moves to a pure helper (`menu.service_groups(services, categories)`), with
  the tab pinned by `ServicesTabTests` before the move. Empty groups then drop
  out on their own.
- **Built in E4: the whole menu is grouped first, then narrowed** to the
  stylist's services (`expert_profile.service_groups`). Grouping only the
  stylist's own services would order the groups by that stylist's first
  service, not the tab's: a group could jump ahead of another compared with
  the tab. This way the groups come in the tab's order for every stylist.
- **Group shape (Q15):** the tab's group is `{id, category_id, name,
  services}`. The profile sends `{id, name, services}`: the same helper, with
  `category_id` left out. No chips (`service_categories`) on this screen.
- **Group ids** are the tab's: the category's UUID, or `other`. Never
  `grp_precision_cuts` as in their example.
- **Rows:** exactly the tab's `_service`: `id`, `name`, `description`,
  `price`, `duration_min`, `duration_max`.
- **Price:** `menu.service_price(svc)`, the branch price when set, as on the
  tab and as booking-api charges. No price by stylist: `service_provider_price`
  exists, booking-api does not read it, and their §7 agrees it is not modelled.
- **Cost:** services 1, categories 1, stages 1, skill bridge 2, staff skills 1.

### 2.3 The `salon` block

Decided (Q16): `{id, name, is_open, hours_today, latitude, longitude}`, values
from the same helpers as `GET /salon/{id}` (`SalonProfileView`,
`SalonProfileSerializer`):

| Key | Helper | Notes |
|---|---|---|
| `id` | the storefront id | echo of the path |
| `name` | `snap_field(snapshot, "IDENTITY", "nameEn")`, else the branch name | same fallback as the profile |
| `is_open` | `hours.resolve(...)["is_open"]` | `true`, `false`, or `null` when the salon published no hours |
| `hours_today` | `hours.resolve(...)["hours_today"]` | "10:00 AM - 9:00 PM". `resolve` says "Closed" on a closed day and when the salon is closed by hand today; **here that becomes `null`** (Rafa). `GET /salon/{id}` keeps sending "Closed" |
| `latitude`, `longitude` | the published `MAP` section, else the branch pin (`get_location`) | `null` when neither has a pin |

Cost: the salon (1 query) and its snapshot (1 query), as the salon profile.

Dated opening-hour exceptions are still not read anywhere (F7). Same gap as
the salon card and the salon profile.

### 2.4 `path_uuid` and the JSON 404s

Same as the service detail: route
`salon/<str:salon_id>/stylist/<str:stylist_id>`, both ids checked by
`path_uuid` (`service_detail_views.py:58`), `NotFound` with a sentence for the
customer. No clash with `salon/<uuid:salon_id>/stylists` (plural).

Sentences: "This salon is not available." (as the service detail) and "This
stylist is no longer at this salon." (theirs, §6).

Our JSON 404 keeps one item in `errors`, as every 404 in this API does:

```
{"detail":"This stylist is no longer at this salon.","code":"not_found",
 "errors":[{"field":null,"code":"not_found","message":"This stylist is no longer at this salon."}]}
```

`path_uuid` moves to a shared module (`params.py`) so two view files do not
import from each other.

### 2.5 Photos: what the platform can hold

- The platform accepts only `image/jpeg`, `image/png` and `image/webp` for
  storefront media (`media-rules.ts:26`, `ACCEPTED_MIME_TYPES`). **No video
  can be there today.** So `type` is `image` for every row now. The "video
  with no thumbnail is left out" rule is written and tested, and has nothing
  to act on until the platform takes video and stores a thumbnail for it.
- The contract wants `thumbnail_url: null` for an image, so the smaller
  copies the platform makes (`GALLERY_THUMB` 400 x 267, `FEED` 1200 x 800, in
  `variants`) are not sent. `url` is the photo itself, as in the salon
  gallery.

### 2.6 Queries for one call (estimate)

| Part | Queries |
|---|---|
| salon | 1 |
| the salon's snapshot (name, hours, pin) | 1 |
| the stylist row | 1 |
| `is_favorite`, our own table (only when signed in) | 1 |
| menu services | 1 |
| categories | 1 |
| stages of the menu | 1 |
| skill bridge (catalog skills, tenant skills) | 2 |
| the stylist's skills | 1 |
| the stylist's photos | 1 |
| shifts for `day_off` | 1 |
| **Total** | **about 11 for a guest, 12 signed in** |

No query per row. For comparison: the service detail is 10, the Services tab 3.

---

## 3. Rules checked against the code

3.1 to 3.4 are what the code does today. 3.5 checks their §3 to §6 rule by
rule. 3.6 lists what differs from their text.

### 3.1 Does a stylist belong to one branch or to the whole business?

**In the platform: one home branch, in practice.**

- `staff_profile.branch_id` is one nullable column. No staff to branch join
  table exists.
- A stylist can only be put on a roster of their home branch:
  `add-roster-member.handler.ts:48-55` refuses anyone whose
  `staff_profile.branch_id` is not the roster's branch
  (`STAFF_NOT_ASSIGNABLE`). A stylist with no home branch cannot be rostered.
- `user_branch_access` and `branch_membership` are about which branches a
  person may **log in to** in the business web. They are keyed by user, not by
  stylist.
- A service belongs to the tenant (one menu for every branch). Skills belong
  to the tenant too.

**What each service does with it:**

| The stylist's home branch | Our `/stylists`, `experts` and this profile (today) | Our nearest available | booking-api create |
|---|---|---|---|
| this salon's branch | shown | offers on days with a shift here | accepted |
| another branch of the same business | **shown** | no offers (no shift at this branch) | **refused**: `stylist_unavailable`, "That stylist does not work at this salon" |
| none (`null`) | **shown** | no offers (cannot be rostered) | **refused**, same |

Why: `salon_stylists` filters by branch only when
`BRANCH_AVAILABILITY_ENABLED` is on, and it is off (`selectors.py:262`,
`285`). booking-api builds its roster from the platform gRPC
`ListStylists(tenant, branch)`, which filters `staff_profile.branch_id`
exactly (`staff-directory.grpc.controller.ts:55-63`, booking-api
`platform-staff-roster.ts:86`). `STAFF_FROM_PLATFORM=true` live (Rafa), so
this table is what happens live.

So for a business with one salon nothing is wrong. For a business with two or
more public salons, every stylist shows at every salon, and only the home
salon can book them. **Live today (Rafa): no business has two public salons,
and no stylist has another or no home branch.** See F1. **Decided (Q3): fixed
in E0, no switch.**

### 3.2 `GET /booking/nearest-available/{salon_id}` with `stylist_id`

It works (`NearestAvailableView`, `views.py:1100`). What it does:

| Point | Today |
|---|---|
| Auth | **Token required** (`IsAuthenticated`). No token: 401 `not_authenticated`. It cannot be made public cheaply: it asks booking-api who is busy with the caller's own token. |
| `from`, `to` | Both required, ISO with an offset, and on the **same salon day**. One day per call. "Next free time this week" is several calls. |
| `stylist_id` alone | Allowed. The visit length is then the browsing default, 30 minutes, and it is sent back as `duration_min`. |
| `stylist_id` + `service_ids` | The stylist must cover **all** the services (one stylist for the whole visit). If not: `offers: []`. |
| Unknown stylist, another salon's stylist, one who left | `200` with `offers: []`. Never 404. |
| `stylist_id` not a UUID | 422 `invalid`. |
| A day counts as working | only with a `shift` row for that date on a roster of **this salon's branch**. No row: no offers. |
| Also needed | the salon open that day, the booking engine's own day window, and booking-api reachable (else 503). |
| `offers[].stylist` | `id`, `name`, `role`, `avatar_url`. No `title`. |
| Grid | 15 minute starts. |

Two gaps worth knowing on this screen:

- The profile is public, the times are not. A guest sees the person and the
  menu, and gets 401 for times. Decided (Q17): it stays so.
- **booking-api does not read the roster at all.** Its stylists come with empty
  hours and no day off from the platform, so it treats a stylist as working
  the whole branch window every day. Our slots are stricter (no shift, no
  offer), which is the safe side. But a day off shown on the profile is not
  enforced when the booking is created. F2.

After E0, nearest available reads the same narrowed list, so a stylist of
another salon gets `offers: []` there for the same reason the profile is 404.

### 3.3 Auth

| Route | Today |
|---|---|
| `/salon/:id`, `/services`, `/service/:id`, `/stylists`, `/products`, `/packages`, `/stories` | `AllowAny`: no token = 200 |
| any `AllowAny` route with a bad or expired token | **401** anyway (simplejwt checks any token sent) |
| `POST /favourite`, `GET /favourite` | `IsAuthenticated`: no token = 401 |
| `/booking/nearest-available/:id` | `IsAuthenticated`: no token = 401 |

Checked locally, no token on `POST /favourite`:

```
401 {"detail":"Authentication credentials were not provided.","code":"not_authenticated",
     "errors":[{"field":null,"code":"not_authenticated","message":"Authentication credentials were not provided."}]}
```

**Decided (Q1): public, token optional**, like the service detail, with one
difference: the answer now depends on who asks (`is_favorite`).
`salon_profile(salon_id, user=request.user)` already does this for the
salon's own heart (`with_is_favorite`): a guest gets `false`, a signed in
customer gets the real value. The app must send its token when it has one, or
the heart shows empty.

### 3.4 Errors

- Unknown salon, unknown stylist, a stylist of another business or another
  salon, one who left, an id that is not a UUID: our JSON 404 `not_found`
  (2.4), with `errors` holding one item, not `[]`.
- A missing token is **not** an error on a public route. A bad token is 401,
  and its `errors` list has the known odd entries (`field: "code"`,
  `field: "token_class"` and so on: simplejwt's dict flattened).

### 3.5 Their §3 to §6, rule by rule

**§3, rules the backend must follow**

| # | Their rule | The code | Verdict |
|---|---|---|---|
| 1 | One call draws the whole screen. No follow-up for media, services or the salon's hours | One view, about 11 queries (2.6), no query per row | Agrees (to build) |
| 2 | `media` is capped at five; `media_count` carries the real number. The grid draws one video, three photos and "+N" | Nothing reads the photos yet. No video can exist (F12) | Agrees for images (E6). A video tile cannot appear today |
| 3 | Distance is the app's job. Send the salon's coordinates | `latitude`, `longitude` from the salon profile's pin | Agrees. `null` when the salon has no pin |
| 4 | Services are this stylist's own, the same filter `/salon/:id/stylists?service_ids=` applies, in reverse. A service they cannot do must not appear | `stylist_service_coverage`, the Expert step's own code (2.2) | Agrees by construction. Note: with `SKILLS_UNVERIFIED=true` booking-api does not check skills at create, so our filter is the only check |
| 5 | Prices are the salon's prices, to the fil, or `amount_mismatch` later | `menu.service_price`: the branch price when set, as booking-api charges | Agrees (since S7) |
| 6 | Durations are a pair of minutes | The tab's row: `service.duration_minutes` twice | Agrees. Always equal, which they allow |
| 7 | A stylist who no longer works there is 404. So is one who works at another salon, even with a real id | Left: not in `salon_stylists`, so 404. Another **business**: 404 (tenant rule). Another salon of the **same business**: **found today** (3.1) | **Disagrees today. E0 fixes it** |
| 8 | `is_favorite` is per customer, from the Bearer token, and `false` when signed out | No stylist favourite exists | Agrees after E3b. "`false` when signed out" means the route must open without a token: Q1 |

**§4, what the screen does next**

| Their text | The code | Verdict |
|---|---|---|
| `salon_id`, the ticked service ids and the stylist `id` go to the booking flow | The ids are the ones the booking sends: storefront id, service ids, `staff_profile.id` | Agrees |
| The flow opens on the Time step and calls `GET /booking/nearest-available/{salon_id}` with `stylist_id`, "exactly as if the stylist had been picked in the Expert step" | 3.2. With `service_ids` too, the stylist must cover all of them. Every ticked service comes from this stylist's own `service_groups`, built by the same coverage code, so they always do | Agrees. Needs a token (the booking needs one anyway), one day per call |

**§5, the heart**

| Their text | The code | Verdict |
|---|---|---|
| `POST /favourite` with `{"stylist_id": "..."}` | Only `salon_id` today | E3b |
| The existing body is unchanged; exactly one of the two ids is sent | | Agrees (E3b): both or neither is 422 |
| It toggles; the answer carries the new `is_favorite` | The salon toggle answers `{"is_favorite": true|false}` | Agrees, same answer |
| Signed out: 401 | `IsAuthenticated` | Agrees today |

**§6, errors**

| Their case | Their status | Ours | Verdict |
|---|---|---|---|
| No such salon | 404 `not_found` | 404 `not_found`, "This salon is not available." | Agrees |
| No such stylist, or not at this salon | 404 `not_found` | 404 `not_found`, "This stylist is no longer at this salon." | Agrees (the same salon, other branch case needs E0) |
| An id in the path is not a UUID | 404 `not_found` | 404 `not_found`, our JSON (`path_uuid`) | Agrees |
| Missing or expired token | 401 | **Missing: 200** (public, Q1). Expired or bad: 401 | Differs on purpose |
| Envelope example: `"errors": []` | | one item in `errors` | Differs, as every 404 of ours (service detail 3.6) |

### 3.6 What differs from their text

Nothing here is a new question: each follows a decision in section 7 or how
the same value already behaves on another screen. All of it goes into our FE
doc (E8).

| Their text | Ours | Why |
|---|---|---|
| Header and §6: a token on every request, 401 when missing | Public, token optional | Q1. Their own §3 rule 8 needs it ("false when signed out") |
| §1: `GET /salon/:id/stylists` "stays as it is" | Two small changes: E0 narrows it to the salon's own stylists (nothing changes live), and E7 fills `day_off`, a key that is already there as `null` | Q3, Q7 |
| `review_count: 320`, `rating: 4.8` | `0`, `null` | Q14, no stylist reviews exist |
| `price_level` 1 to 4 | `null` | Q6 |
| `is_network_member: true` | always `false` | Q8, and we ask them |
| `day_off`: one full day name | one name, or several joined: "Friday, Saturday" | Q7 |
| `salon.is_open` boolean | can be `null` (no hours published) | Q16: the same value as `GET /salon/{id}` |
| `salon.latitude`, `longitude` numbers | can be `null` (no pin) | Q16 |
| `name` string | can be `null` | the same as the Expert step row |
| group id `grp_precision_cuts` | the tab's ids: a UUID or `other` | Q15: "grouped and ordered as the Services tab" |
| a `video` item with a `thumbnail_url` | images only | F12 |
| `errors: []` on a 404 | one item | every 404 of ours |
| example URLs on `cdn.gostyle.uk` | plain S3 URLs | the app must not assume a host |

---

## 4. `POST /favourite` with `stylist_id`

### 4.1 How favourites work today

- Model `Favourite` (`apps/accounts/models.py:206`), table `consumer.favourite`,
  owned by our migrations: `account` (FK, cascade), `storefront_id` (plain
  UUID, no FK because the salon lives in the platform's tables),
  `created_at`. Unique on `(account, storefront_id)`.
- `POST /api/v1/favourite` body `{"salon_id": "..."}`. A toggle: delete the
  row if it is there, else create it. Answer `{"is_favorite": true}` or
  `{"is_favorite": false}`, status 200.
- The salon is **not** checked for existence (on purpose, one query saved).
- `GET /api/v1/favourite`: the customer's saved **salons**, paginated, with
  the discovery filters.
- Signed out: 401 (3.3).
- The salon profile's `is_favorite` reads this table. The discovery card has
  no `is_favorite` field.
- There are no tests for favourites today.

### 4.2 What a stylist favourite needs (Q11, Q12, Q13: all yes)

| Need | Decided |
|---|---|
| Storage | A **new model and table** in our own schema: `FavouriteStylist` (`consumer.favourite_stylist`): `account` (FK, cascade), `staff_id` (plain UUID), `created_at`, unique on `(account, staff_id)`, index `(account, -created_at)`. One migration, `accounts/0012`, only a `CreateModel`. |
| Why not a new column on `favourite` | `storefront_id` is NOT NULL and the unique rule is on it. Changing both is a riskier migration, and after a rollback the old code would read stylist rows as salons. A new table is additive: old code never sees it. |
| Request | `{"stylist_id": "..."}`. Exactly one of `salon_id` and `stylist_id` (their §5). Both: 422, no field, code `one_id_only`, "Send salon_id or stylist_id, not both.". Neither: today's answer (422 on `salon_id`, "This field is required."). |
| Answer | The same: `{"is_favorite": true}` or `{"is_favorite": false}`, 200. |
| Signed out | 401, already the case for the route. |
| Bad id | 422 `invalid`, for `stylist_id` and for `salon_id` (F3). |
| Does the stylist exist | Checked on **save** only (one query): a stylist the app cannot show at any salon = 404 "This stylist is no longer available.". The rule is `salon_stylists` without the salon (`selectors.showable_stylists`: employed, joined, not deleted, a home branch, a live login), and `salon_stylists` is now built on it. Never on unsave, so a heart on a stylist who left can still be removed. The salon heart keeps skipping this check. |
| `is_favorite` on the profile | `false` for a guest, with no query. Signed in: one small read of our own table (`selectors.is_favourite_stylist`). **Changed in E3b from "one `Exists` on the stylist row":** as its own read it is tested end to end on the real test database (the platform's tables are not in it), at the cost of one query for a signed in customer. |
| A list of saved stylists | Not there, and the contract does not ask. `GET /favourite` stays salons only. |
| Deploy | Migrations run when the container starts. The table is new, so a rollback to the old image is safe. |

---

## 5. MISSING fields: owner, and what we send now

| Field | Who would add it | We send | Allowed by the contract? |
|---|---|---|---|
| `bio` | Platform: a column on the staff record and a field in the business web staff form. Its gRPC already has an empty `bio` slot | `null` | Yes: "null hides the block" |
| `years_experience` | Platform: a column (years, or a career start date) and a form field. gRPC slot exists, empty | `null` | Yes: "null when the salon did not fill it in" |
| `price_level` | Product first: what does it mean for a person? Their §7 says it can be dropped if it mirrors the salon | `null` | Yes: "null hides the row" |
| `is_network_member` | Product first: what does it mean? Our FE doc asks them | `false` | A boolean is what they ask for |
| `rating`, `review_count` | Platform: a stylist on reviews | `null`, `0` | Yes: "null when nobody has rated them", "0 shows No reviews yet" |
| stylist stories | Platform: a stylist on stories | `has_story` = has media | Yes: their §7, "today both would read media" |
| `is_favorite` | **Us**: the new table (section 4) | real value | |

`day_off` and the photos are SHAPE, not MISSING: see Q7 and Q9.

---

## 6. Findings

- **F1. Stylists show at every salon of a business; only the home salon can
  book them (a bug in the Expert step, not live today).** See 3.1.
  `salon_stylists` is tenant wide while `BRANCH_AVAILABILITY_ENABLED` is off,
  and that flag cannot simply be switched on: it also turns on the services
  filter that would empty menus (service detail audit, F2). The stylist rule
  gets its own place. **Fixed in E0.** Live (Rafa): 0 pairs affected, because
  no business has two public salons yet.
- **F2. booking-api ignores the roster and any day off.** The platform's
  `ListStylists` sends `offday`, `opening_time` and `closing_time` empty, and
  booking-api never calls `ListShifts`. So it accepts a booking on a day the
  stylist has no shift. Our nearest available does not offer such a day, so
  the app never suggests it. Known on the booking-api side (its "ask A3").
  Not this screen's to fix, but a "day off" label is a display hint, not a
  block.
- **F3. `POST /favourite` with a `salon_id` that is not a UUID answers 500.**
  Checked locally: `Favourite.objects.filter(storefront_id="not-a-uuid")`
  raises Django's `ValidationError`, which DRF does not handle. **Fixed in
  E3a**: 422 `invalid`.
- **F4. Double tap on the heart can answer 500.** Two requests at once: both
  delete nothing, both create, the second hits the unique rule
  (`IntegrityError`). Read from the code, not reproduced. **Fixed in E3a** for
  the salon heart, and built in from the start for the stylist heart.
- **F5. `discoverable_salons()` has a wrong `is_favorite` annotation.** It is
  "has a live certification" (`selectors.py:1013`, a copy of the lines above
  it). `salon_profile` overwrites it with the real one, and no other
  serializer reads it, so nothing shows wrong today. Misleading; remove it in
  a tidy-up. Not in this plan.
- **F6. We show a stylist whose login account is deleted.** `salon_stylists`
  reads `user_account` without checking `deleted_at`. The platform gRPC drops
  such a person, so booking-api cannot book them. **Fixed in E0** (Rafa, Q3).
  Live (Rafa): 0 such stylists.
- **F7. Dated opening-hour exceptions are still not read**, and the comment in
  `SalonProfileView` says the table does not exist. It does
  (`storefront_status_exception`, model already in `platform_data`). So
  `is_open` on this screen has the same holiday gap as the salon card. Not in
  this plan.
- **F8. One salon, three spellings.** Open now is `open` on the card and
  `is_open` on the profile. The pin is `coordinate` on the card,
  `coordinates` in favourites, and `location.latitude/longitude` on the
  profile. Decided for this screen: their names (Q16).
- **F9. `storefront_media.staff_id` is never checked.** Only
  `PATCH /v1/storefront/media/:id` sets it, the handler says "NOT validated,
  and that is a decision", and nothing on the platform reads it back. It may
  hold an id that is not a stylist. We match it to the stylist's own id inside
  this salon and ignore the rest.
- **F10. The platform's own gallery feed also asks for
  `processing_state = PROCESSED`.** Our salon gallery and service photos do
  not (Rafa, service detail S3). The stylist's photos follow those two (Q9).
- **F11. A stylist's title has three homes on the platform:**
  `staff_profile.position`, `user_account.job_title`,
  `tenant_user_profiles.job_title`. We use the first as `title` and the second
  as `role`. Fine, as long as this screen uses the Expert step's code.
- **F12. No video can be in `storefront_media` today.** The platform accepts
  three image types only (2.5). `type` is always `image` until that changes.
- **F13. `GET /stylists?tenant_id=&branch_id=` ignores its `branch_id`.** The
  parameter is required, then not used: the view takes the first public salon
  of the tenant (`views.py:576`). Harmless while the list is tenant wide. Once
  E0 narrows the list to a salon's own branch, this route must find the salon
  by `branch_id`, or a business with two salons gets the wrong salon's
  stylists. **Fixed in E0** (Rafa). `GET /services?tenant_id=` picks the salon
  the same way, so since S7 it can show the wrong branch's price: **its own
  small last step, E9** (Rafa).

---

## 7. Questions for Rafa (answered 2026-09-30)

| # | Question | My recommendation | Rafa's answer |
|---|---|---|---|
| Q1 | Auth: public with an optional token? | Yes, `AllowAny`, like the service detail | **Yes.** Public, token optional, `is_favorite` `false` for a guest |
| Q2 | Which stylists answer 200? | Exactly the Expert step's people. Anyone else 404 | **Yes.** Anyone else: 404 "This stylist is no longer at this salon." (their sentence) |
| Q3 | Fix F1 first: a stylist shows only at their home salon? | Yes, as E0, its own commit, its own switch, after SQL B | **Yes, E0 is needed** (their §3: a stylist of another salon is 404). The same home branch rule on `/stylists`, `experts` and nearest available, so all agree. **Also hide a stylist whose login account is deleted (F6), as the platform does.** **After the live SQL: no switch** (E0 changes nothing live today) |
| Q4 | `bio` | `null`, platform ticket | **`null`** |
| Q5 | `years_experience` | `null`, never from `created_at` | **`null`** |
| Q6 | Price level | `null`, ask the app team | **`price_level: null`** (hides the row). Their own open point says it can be dropped |
| Q7 | `day_off` | From the roster, only when steady: no shift on that weekday in every rostered week of the last 4, at least 2 rostered weeks. Same helper for the Expert step rows | **Yes, with two additions: only among the days the salon is open** (a day the salon is closed is not a day off), and several days read **"Friday, Saturday"**. Else `null`. Same helper for the Expert step rows. **Final yes after the live SQL** (rosters are filled) |
| Q8 | "Network member" | Ask what it means; until then `false` | **`is_network_member: false`.** Our FE doc asks them what it should mean |
| Q9 | Photos source | This salon's `storefront_media` with `staff_id` = the stylist: GALLERY, public, APPROVED, not deleted; order `sort_order`, `created_at`, `id` | **Yes, but newest first as the contract says, at most 5, `media_count` counts all. `type` from `mime_type`; a video with no thumbnail is left out** |
| Q10 | Stories | `[]` | **`has_story` = the stylist has media** (their open point: the avatar ring and the grid both read media today) |
| Q11 | Favourite storage | A new table `favourite_stylist` | **Yes** |
| Q12 | Check the stylist exists when saving a heart? | Yes, on save only: 404. Never on unsave | **Yes** |
| Q13 | Fix F3 and F4 for the salon heart in the same step? | Yes, as its own small commit | **Yes** |
| Q14 | `rating`, `review_count` | `null`, `null`, as the Expert step | **`rating: null`, `review_count: 0`** (the contract's types, like the service detail) |
| Q15 | `service_groups` content | Only menu services this stylist can do alone; same groups, order and price as the tab; no empty groups; `[]` when none | **Yes.** Groups as `{id, name, services}`, rows exactly the Services tab's |
| Q16 | `salon` block names | The contract's names, values from the card helpers | **Their names (`id`, `name`, `is_open`, `hours_today`, `latitude`, `longitude`), values from the same helpers as `GET /salon/{id}`.** `hours_today` like "10:00 AM - 9:00 PM", `null` when closed today |
| Q17 | Times for a guest | Keep nearest available behind a token | **As recommended** |
| Q18 | "Next available" across days | The app calls nearest available day by day for now | **As recommended** |
| Q19 | Behind a flag? | No flag for the new route, none for `stylist_id` on the heart. E0 per Q3 | **As recommended.** E0: no switch |

### 7.1 Where the contract changed a recommendation

Confirmed against the contract text.

| # | I recommended | The contract says | So now |
|---|---|---|---|
| Q2 | a customer sentence of ours | §6: "This stylist is no longer at this salon." | their sentence, word for word |
| Q3 | E0 "if you say yes", maybe behind a switch | §3 rule 7: "So is one who works at another salon, even with a real id" | E0 is required for this screen to follow the contract. No switch (live SQL) |
| Q6 | `null` and ask the app team | §2: "null hides the row". §7: "If it always mirrors the salon it can be dropped and read from the profile instead" | `null`, no question to them |
| Q9 | the salon gallery's order (`sort_order`, then oldest) and no cap | §2: "newest first. At most 5"; `media_count` "how many exist in all"; items `{id, type, url, thumbnail_url}`, `thumbnail_url` null for an image | `created_at` descending; capped at 5; `media_count`; `type` from `mime_type`; `thumbnail_url: null` |
| Q10 | a stories list, empty | §2: `has_story` boolean. §7: "today both would read media" | `has_story` = `media_count > 0` |
| Q14 | `review_count: null`, the same as the Expert step row | §2: a number, "0 shows No reviews yet" | `0` on the profile. The Expert step row stays `null` |
| Q15 | the tab's groups as they are | §2: groups `{id, name, services}`, "the same fields as /salon/:id/services" | `category_id` left out of each group, no chips |
| Q16 | card helpers | §2: `id`, `name`, `is_open`, `hours_today`, `latitude`, `longitude` | the salon **profile's** helpers (published name, `MAP` pin over the branch pin), and `hours_today` is `null` when closed |

Not from the contract, from Rafa: Q7's "only among the days the salon is
open", and F6 joining E0.

### 7.2 Small readings of mine inside the answers

**All eight: yes (Rafa, 2026-09-30).**

1. **Q3, "login account is deleted":** the platform's test, exactly: a
   `user_account` row with `id = staff_profile.user_id`, the **same tenant**,
   `deleted_at` null. The account's `status` (SUSPENDED, DISABLED) is not
   checked, because the platform's list does not check it either.
2. **Q3, a stylist with no home branch** (`branch_id` null): hidden.
   booking-api refuses them and the platform cannot roster them.
3. **Q7, the 4 weeks:** the 4 roster weeks that end with the current week, on
   the salon's own date. A rostered week = a week with at least one shift at
   this salon's branch.
4. **Q7, a salon with no published hours:** `day_off` is `null`. We cannot
   tell a closed day from a day off.
5. **Q7, the text:** English weekday names, Monday to Sunday order, joined
   with ", ".
6. **Q9, `type`:** `image` when `mime_type` starts with `image/`, `video` when
   it starts with `video/`, anything else left out.
7. **Q9, `media_count`:** counts the rows that pass the same rules as `media`
   (so a left-out video is not counted).
8. **Q16, "closed today":** a closed weekday **and** a salon closed by hand
   today both give `hours_today: null`.

---

## 8. Build plan (customer-api only)

Each step: its own commit on `feat/expert-profile`, tests first, suite run
before the commit. Baseline: 840 tests, the same 18 known failures. Platform
tables are unmanaged, so those tests mock the selectors (`SimpleTestCase`,
the `Seams` pattern of `test_service_detail.py`). The favourite table is ours
and managed, so its tests use the real test database. `urls.py` and
`selectors.py` use CRLF: keep `\r\n`.

| Step | What | Files | Tests |
|---|---|---|---|
| E0 | **Built, reviewed by Rafa (2026-09-30).** **F1, F6, F13 (Q3). No switch.** `salon_stylists` keeps only a stylist whose home branch is this salon's branch (`staff_profile.branch_id = salon.branch_id`, null hidden) and whose login account is live (same tenant, not deleted). It is the shared list, so `/stylists`, `experts`, nearest available, the group booking check and the routine avatars all change together. `StylistListView` finds its salon by `branch_id`. The rule does **not** use `BRANCH_AVAILABILITY_ENABLED` (that flag also hides services, service detail F2) | `selectors.py` (`salon_stylists`), `views.py` (`StylistListView`) | home branch = this salon: shown; another branch: gone from `/stylists`, `experts`, nearest available (`offers: []`) and refused by the group check; no home branch: hidden; deleted login, missing login, login of another tenant: hidden; a business with one salon: unchanged; `/stylists?tenant_id=&branch_id=` answers that branch's salon, unknown branch 404 |
| E1 | **Built, reviewed by Rafa (2026-09-30).** Route `salon/<str:salon_id>/stylist/<str:stylist_id>`, view skeleton, 404s, `AllowAny`. `path_uuid` moved to `params.py`. `FLOW` entry in `config/openapi_flow.py` | `urls.py`, new `expert_profile_views.py`, `selectors.py` (new `stylist_for_salon`), `params.py` | non-UUID in each position = JSON 404; unknown salon "This salon is not available."; another tenant's stylist, another salon's stylist, INVITED, INACTIVE, ARCHIVED and deleted = 404 "This stylist is no longer at this salon."; no token 200; bad token 401; `test_openapi_flow` green; service detail tests green after the `path_uuid` move |
| E2 | **Built, reviewed by Rafa (2026-09-30).** The person: `id`, `salon_id`, `name`, `title`, `role`, `avatar_url`, `rating: null`, `review_count: 0` | new pure `apps/salons/expert_profile.py`, view | `name`, `title`, `role`, `avatar_url` equal to that stylist's Expert step row on the same mocks; `null` name, title, role, avatar; `review_count` is `0` here while the list row keeps `null` |
| E3a | **Built, reviewed by Rafa (2026-09-30).** **F3, F4:** the salon heart answers 422 for a bad id and never 500 on a double tap. Two extras, accepted by Rafa: a number as `salon_id` is 422 (it used to save as "the UUID numbered 123"), and a body that is not a JSON object is 422 (was 500). Today's answers pinned first | `views.py` (`FavouriteListView.post`) | save, unsave, missing id 422 (today's), bad id 422 (new), unique clash answers `true`, no token 401 |
| E3b | **Built, reviewed by Rafa (2026-09-30).** Stylist heart (their §5): model `FavouriteStylist`, migration `accounts/0012`, `stylist_id` on `POST /favourite`, `is_favorite` on the profile | `accounts/models.py`, migration, `views.py`, `selectors.py` | save then unsave; both ids 422; neither 422; bad id 422; unknown stylist 404 on save, not on unsave; no token 401; the salon heart unchanged; two customers do not see each other's heart; `is_favorite` true, false, guest `false`; `makemigrations --check` clean |
| E4 | **Built, reviewed by Rafa (2026-09-30).** `service_groups`: the tab's grouping moves to `menu.service_groups` (tab pinned first); the profile sends the covered services only, groups as `{id, name, services}` | `menu.py`, `views.py` (tab), `expert_profile.py`, view | tab answer unchanged after the move; only covered services; each row equal to the tab's row for that service (same keys, same branch price, 0 means 0); group order as the tab; no `category_id`; no empty group; a no-stage service absent; a stylist with no skills `[]`; agrees with `/stylists?service_ids=` for every menu service |
| E5 | `salon`: `{id, name, is_open, hours_today, latitude, longitude}` from the salon profile's helpers | `expert_profile.py`, view | equal to `GET /salon/{id}` for `name`, `is_open` and the pin on the same mocks; open day "10:00 AM - 9:00 PM"; closed weekday `null`; closed by hand `null`; no hours published: `is_open` and `hours_today` `null`; overnight hours; `MAP` pin wins over the branch pin; no pin `null`, `null` |
| E6 | `media` as `{id, type, url, thumbnail_url}`, `media_count`, `has_story` (Q9, Q10) | `selectors.py` (new `stylist_media_rows`), `expert_profile.py` | newest first, `id` breaks a tie; cap 5 with `media_count` 7; deleted, private, unapproved left out; another salon's and another tenant's left out; a `staff_id` that is not this stylist left out; an image: `type` `image`, `thumbnail_url` `null`; a video left out and not counted; unknown file type left out; none: `[]`, `0`, `has_story: false`; one photo: `has_story: true` |
| E7 | `day_off` (Q7, final yes). A pure rule plus one selector (shifts of the listed stylists at this branch, 4 roster weeks). Used by the profile **and** by `stylist_rows`, so the Expert step and `experts` fill it too (the list gains 2 queries: shifts, and the salon's snapshot for its open days) | `selectors.py`, new pure rule in `expert_profile.py` (or `roster.py`), `views.py` (`stylist_rows`, `_stylist_row`) | one steady day "Tuesday"; two "Friday, Saturday" in week order; a day the salon is closed is never a day off, with or without a shift; works every open day `null`; off in one week only `null`; 1 rostered week `null`; no shifts `null`; salon hours not published `null`; shifts at another branch ignored; the list row and the profile give the same text |
| E8 | The empty fields: `bio: null`, `years_experience: null`, `price_level: null`, `is_network_member: false`. OpenAPI (`extend_schema`, response serializer, a real example). Our FE doc `docs/EXPERT_PROFILE_API.md`: every field, what differs from their text (3.6), and the question to them about `is_network_member` | view, new `expert_profile_serializers.py`, `docs/` | keys present with those values; the example is the view's real answer; `manage.py spectacular` builds with no new warning; the FE doc's JSON blocks parse |
| E9 | **F13's twin (Rafa): `GET /services?tenant_id=&branch_id=`** finds its salon by `branch_id` when one is sent, so the price is that branch's. Its own small last step | `views.py` (`ServiceListView`) | with `branch_id`: that branch's salon and price; unknown branch 404; without `branch_id`: today's answer; a business with one salon: unchanged |

Order: E0, E1, E2, E3a, E3b, E4, E5, E6, E7, E8, E9. E0, E3a, E4, E7 and E9
change or touch existing answers, so each stays a separate commit you can
review or leave out. One migration (ours, additive). Nothing to deploy in
booking-api or the platform.

What our FE doc will tell the app team (E8): everything in 3.6, plus: the app
must send its token when it has one or the heart shows empty; times need a
token and one call per day; `has_story` follows `media`; a day off is a hint,
the booking is still checked when it is created.

Live check after deploy (one curl at a time, your token):
`GET /api/v1/salon/{salon}/stylist/{stylist}` without a token, the same with
a token, `POST /api/v1/favourite` with `stylist_id` twice, then a non-UUID id.

---

## 9. Read-only checks for the server

**Database.** `scripts/sql/expert_profile_live_checks.sql` (committed with E0).
One `SELECT` in a read-only transaction that ends with `ROLLBACK`. It ran
clean on the local database.

```
psql -h <DB_HOST> -p <DB_PORT> -U <DB_USER> -d <DB_NAME> \
     -v ON_ERROR_STOP=1 -f expert_profile_live_checks.sql
```

| Section | What it tells us | Decides |
|---|---|---|
| A stylists | How many stylists the app shows, and how many have a name, a title, a role, an avatar; how many have a deleted or missing login account | how empty the screen looks; how many E0 hides for F6 |
| B branch | Businesses with 2 or more public salons, and salon + stylist pairs where the stylist's home branch is another branch or none | Q3: switch or no switch for E0 |
| C photos | Photos tagged with a stylist, by kind and file type, how many the app could show | Q9, Q10 (how many rings light up) |
| D stories | Live stories, and whether any can be tied to a stylist | background for Q10 |
| E day off | With Q7's own rule (4 roster weeks, only days the home salon is open): how many stylists get no day off, one, several; how many have too little roster to tell | Q7: the final yes |
| F price | Whether any salon uses a price by stylist | the note in 2.2 |
| G services | Stylists who can do no service (their menu would be empty), and the spread | how many profiles open with `service_groups: []` |

**Live results (Rafa, 2026-09-30):**

| What | Live |
|---|---|
| Public salons | 3 |
| Businesses with 2 or more public salons | 0 |
| Stylists whose home branch is another branch, or none | 0 |
| Stylists with a deleted login account | 0 |
| Shifts in the 4 roster weeks | 113 |
| Stylists rostered | 6 of 7 |
| booking-api `STAFF_FROM_PLATFORM` | `true` |
| booking-api `SKILLS_UNVERIFIED` | `true` |
| Calls to `GET /stylists?tenant_id=&branch_id=` in the last 2 weeks (live logs) | 0 |

So: E0 changes no live answer today (no switch), the new 404 on
`GET /stylists?tenant_id=&branch_id=` (F13) touches nobody, and rosters are
filled enough for `day_off` (Q7 final yes).

**booking-api switches.** The line that reads them, printing only the two:

```
docker service inspect gostyle-booking_api \
  --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}' \
  | grep -E '^(STAFF_FROM_PLATFORM|SKILLS_UNVERIFIED)='
```
