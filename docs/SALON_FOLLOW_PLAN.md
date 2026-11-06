# Follow / Unfollow a salon: plan

Date: 2026-11-06. Status: **plan only, nothing built.** Waiting for Rafa's
answers (D1 to D9, section 4).

If the mobile team sends a brief for this, the brief wins, built same to same,
and it replaces the choices below.

---

## 0. In short

- **In the Figma, following a salon is the heart.** The "Final Design" file
  (yKWg1EfK9EaaoKR1t8fgbO, all ~400 screens searched) never uses the word
  "follow". It has the heart on the salon, a **Favorites** tab under My Profile
  (frame 2696:68316), and on Notification Preferences: **"Special Offers: Deals
  from your favourite salons"** (frame 2696:55055).
- **Half of it is live already**: the heart (`POST /favourite`), the Favorites
  list (`GET /favourite`) and `is_favorite` on the salon profile.
- **What is missing**: a safe Follow button call (today's is a toggle), a
  follower count, the heart state on lists, newest-first order, and the real
  point of following: **telling followers when the salon posts an offer**.
  Later, the salon seeing its follower count.
- No booking-api change. No platform change for phases 1 to 4.

---

## 1. What exists today

| What | Where | State |
| ---- | ----- | ----- |
| Table of hearts: one row per customer and salon (storefront), unique | `consumer.favourite`, `apps/accounts/models.py:387` | Live |
| Heart a salon: a **toggle** (on if off, off if on), answers `{"is_favorite": bool}`. The salon id is not checked | `POST /favourite` `{"salon_id"}`, `apps/salons/views.py:1580` | Live |
| Heart a stylist (own table `favourite_stylist`) | `POST /favourite` `{"stylist_id"}` | Live, not part of this plan |
| Favorites list: salon cards, sorted by salon id (not newest first) | `GET /favourite`, `apps/salons/views.py:1529` | Live |
| `is_favorite` on the salon profile (false for a guest) | `GET /salon/{id}`, `selectors.with_is_favorite` | Live |
| Hearts removed when an account is purged (day 30) | `apps/accounts/models.py:211` | Live |
| Switch "Special offers", default off | `NotificationPreference.special_offers` | Live, but nothing sends offers |
| Notification kind `promo.offer` (Promotions tab, push + email, kept only when the switch is on) | `apps/notifications/registry.py:77` | Registered, no producer |
| Inbox with a per-account duplicate key, push worker | `apps/notifications/` | Live |
| Salon offers ("20% off colour", dated, at most a few at once, written live, no Publish) | platform `storefront_offer`, read-only here | Exists, not used by our API |
| Story rail | `GET /discover/story` | Live, not personal |

---

## 2. Gaps

| # | Gap | Why it matters |
| - | --- | -------------- |
| G1 | The heart call is a **toggle** | A Follow button that retries, or a double tap, flips back. A button labelled Follow / Following needs "set on" and "set off" calls that are safe to repeat |
| G2 | Saving a heart does **not check the salon** | A wrong id is stored forever and would count as a follower |
| G3 | **No follower count** anywhere | The profile cannot show "1.2k followers"; the salon cannot see it |
| G4 | **Lists do not say if you follow** (discover, map, search, stories) | The app cannot draw a filled heart on a card |
| G5 | Favorites list is sorted by **salon id** | Newest followed first is what a customer expects |
| G6 | **Nothing tells followers anything** | The Figma's "Deals from your favourite salons" switch does nothing today. The platform sends no event when a salon posts an offer |
| G7 | The **salon side sees nothing** | No follower count in the partner app |
| G8 | Dead code: the card query has an `is_favorite` worked out from **certifications** (`selectors.py`, in `with_published_card_fields`) | Never sent today, but anyone who sends it would show wrong hearts. Remove it |
| G9 | An account waiting for deletion (30 days) would still count | Counts should be real people who are still here |

---

## 3. The plan, in phases

Each phase is its own PR (base main), its own switch (off), its own tests,
and a short FE guide section in `docs/SALON_FOLLOW_API.md`.

### Phase 0: clean up (small, no switch)

1. Remove the dead certification `is_favorite` from the card query (G8).
2. Migration: index on `favourite (storefront_id)`, so counting a salon's
   followers is one fast index read. Built concurrently (no table lock).
3. Read-only live check, before phase 1:

   ```sql
   -- How many hearts, how many salons, and how many hearts point at a salon
   -- the app cannot show (G2).
   SELECT count(*)                                   AS hearts,
          count(DISTINCT f.storefront_id)            AS salons,
          count(*) FILTER (WHERE s.id IS NULL)       AS hearts_on_unknown_ids
   FROM consumer.favourite f
   LEFT JOIN public.storefront s ON s.id = f.storefront_id;
   ```

### Phase 1: the Follow button (switch `SALON_FOLLOW_V1`)

New calls, safe to repeat. `POST /favourite` stays exactly as it is for the
app builds already installed; all of them write the same table, so the heart
and the Follow button always agree.

| Method | Path | Answer |
| ------ | ---- | ------ |
| `PUT` | `/salon/{salon_id}/follow` | `200 {"is_following": true, "followers_count": 128}` |
| `DELETE` | `/salon/{salon_id}/follow` | `200 {"is_following": false, "followers_count": 127}` |

Rules:

1. Sign-in needed: a guest gets `401` (the app asks them to log in).
2. `PUT` twice is still one follow; `DELETE` twice is still unfollowed.
3. `PUT` checks the salon is one the app can show, else `404` "This salon is
   not available." `DELETE` never checks: unfollowing a salon that has gone
   must always work (same rule as the stylist heart).
4. A double tap cannot make two rows (the table's unique rule, plus
   get-or-create, as the heart does today).
5. Throttle: 60 calls a minute per customer (new rate `follow`).
6. `followers_count` counts **active** accounts only: not deleted, not
   waiting for deletion (G9).

The salon profile `GET /salon/{id}` gains two keys (old keys stay):

| Key | Meaning |
| --- | ------- |
| `is_following` | Same value as `is_favorite`. `false` for a guest |
| `followers_count` | As above. Shown to everyone, guests too |

Switch off: the two routes answer `404` and the profile is as today.

### Phase 2: lists (switch `SALON_FOLLOW_LISTS_V1`)

1. `is_following` on every salon card for a signed-in customer (`false` for a
   guest, no extra query): `/discover`, `/discover/map`, `/search/salons`,
   `/discover/story`. One small check per list, not one per card.
2. `GET /favourite?sort=recent`: newest followed first. Without `sort`, the
   order stays as today, so installed builds see no change.
3. No count on lists (it would cost a count per card for little use).

### Phase 3: offers to followers (switch `FOLLOW_OFFERS_PUSH_V1`)

The Figma's "Special Offers: Deals from your favourite salons".

1. The notification worker looks at `storefront_offer` every 5 minutes
   (no platform change): offers created in the last day, not deleted, not
   already ended (`ends_on` is today or later, on the salon's own date).
2. For each new offer, every follower of that salon with **Special offers on**
   gets a `promo.offer` item: title "{Salon name} has a new offer", body the
   offer text ("20% off colour today"), target the salon, gone from the inbox
   after `ends_on`. Push only when their push switch is on.
3. Each customer gets each offer once: the inbox's duplicate key is
   `offer:{offer_id}`, so a rerun or a second worker sends nothing twice.
4. Sent in batches of 500 followers.
5. Frequency cap (D6): at most one offer **push** per salon per customer per
   day; the inbox still keeps every offer.
6. An offer that starts later is sent when it is posted, with its dates in
   the text ("from 12 Nov").

### Phase 4: stories from salons you follow (switch `FOLLOW_STORIES_FIRST_V1`)

`GET /discover/story` for a signed-in customer: salons they follow first,
then the rest, as today. Guests: no change.

### Phase 5: the salon sees its count (later, other teams)

1. New gRPC call on our server (beside `ConsumerDirectory`, same internal
   key): `GetFollowerCount(storefront_id)`.
2. The platform team shows it in the partner app.
3. Count only. **No follower names** to salons (privacy: "Show my name to
   salons" exists, but following is not a booking).
4. Business web rules apply: separate PRs, additive only.

---

## 4. Decisions for Rafa

| # | Question | Recommended |
| - | -------- | ----------- |
| D1 | Is Follow the **same as the heart** (one table, one state), or a separate thing with its own button? | **Same.** The Figma has only the heart and the "favourite salons" offer switch. If the mobile team designs a separate Follow button, it becomes a new table (additive, like `favourite_stylist`) |
| D2 | New safe calls `PUT` / `DELETE /salon/{id}/follow` with `is_following`, keeping `POST /favourite` as it is? | **Yes** |
| D3 | Show `followers_count` to everyone, guests too? | **Yes, send it**; the app decides whether to draw it (for example, hide when under 10) |
| D4 | Count active accounts only (not deleted, not waiting for deletion)? | **Yes** |
| D5 | Offers: the worker reads `storefront_offer` every 5 minutes, or ask the platform team for an event? | **Read it ourselves** (no other team, live in one PR). An event can replace it later |
| D6 | Cap offer pushes? | **One push per salon per customer per day**; the inbox keeps all |
| D7 | Stories from followed salons first? | **Yes, phase 4** |
| D8 | What the salon sees | **Count only**, phase 5, no names |
| D9 | Following is per branch (each storefront), as the heart is today. A business with two branches is two follows | **Yes, per branch** |

---

## 5. Risks and how they are handled

| Risk | Handling |
| ---- | -------- |
| Old builds use the toggle, new builds the Follow calls | One table: both always agree |
| A retry flips the state | `PUT` / `DELETE` are safe to repeat |
| Counting is slow on a big salon | Index on `storefront_id` (phase 0); count only on the profile and the follow answer |
| A follow to a salon that later closes or unpublishes | Row kept, hidden from lists (lists only show salons the app can show), count unchanged; it comes back if the salon returns |
| Offer spam | Only followers with the switch on; one push per salon per day; once per offer |
| Two workers send the same offer | The inbox's unique key per account |
| Deleted accounts | Purge already removes hearts; pending deletion is left out of counts |
| Privacy | No names to salons; count only |

---

## 6. Tests (each phase)

- Follow: guest 401; `PUT` twice one row; `DELETE` twice fine; `DELETE` on a
  gone salon 200; `PUT` on an unknown salon 404; the toggle and the new calls
  agree both ways; double tap at once makes one row; count leaves out
  pending-deletion and deleted accounts; throttle 429; switch off 404.
- Profile: `is_following` equals `is_favorite`; guest false with no query;
  switch off: the answer is exactly as before.
- Lists: one query for the whole page; guest no query; `sort=recent` order;
  no `sort` unchanged.
- Offers: new offer to switched-on followers only; switch off nothing kept;
  rerun sends nothing twice; ended or deleted offer never sent; cap of one
  push per salon per day; batches; salon's own date at the day edge.
- Stories: followed first, then the old order; guest unchanged.
- Docs: every example in the FE guide is a real answer (as Service Detail).

---

## 7. Order

Phase 0, then 1 (the button and the count), then 2, then 3. Phases 4 and 5
when wanted. After each phase: Rafa merges, deploys, switches on, and we test
live together.
