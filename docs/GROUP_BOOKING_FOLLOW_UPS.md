# Group booking: follow-ups for later

Things found while building mobile group booking (2026-09-25) that are NOT part
of it. Each touches something shared with the business web, so each needs its
own PR and Rafa's OK. Newest decisions first.

## Security

| # | What | Where | Status |
| - | ---- | ----- | ------ |
| S1 | A customer token could cancel, move, check in or no-show ANY booking by id, and could send `nowMs` to choose the clock its refund is measured against. | booking-api `lifecycle.controller.ts` | **Fixed** in booking-api branch `fix/lifecycle-customer-ownership` (commit `5f01d92`). A customer now acts on their own booking only (404 otherwise) and `nowMs` is ignored for customers. Staff unchanged. Needs its own PR and deploy. |
| S2 | `POST /v1/groups/:id/confirm` and `DELETE /v1/groups/holds/:holdId` do not check that the caller made the party. | booking-api `groups.controller.ts` | Open. Business routes; the mobile path no longer calls them once `GROUP_BOOKING_V2` is on. |

## Business group confirm (desk parties)

| # | What | Where | Status |
| - | ---- | ----- | ------ |
| B1 | It re-points chair reservations by start minute only, so every chair of an arrive-together party ends up on the FIRST member's booking. Cancelling that member frees all the party's chairs. | booking-api `group-confirm.repository.ts` | Open. The mobile confirm does not have this: it re-points each member's own reservation by id. |
| B2 | It does not check that a stylist reservation was actually re-pointed (`updateMany` count), so a re-plan that picks a different stylist can leave a member holding no time. | same file | Open. |
| B3 | It writes no `booking_status_history` rows. | same file | Open. |

## Mobile group booking

| # | What | Status |
| - | ---- | ------ |
| M1 | Pay a party in the app (`PATCH` by group id). | Another team's work. Parties are saved to pay at the salon until then. |
| M2 | booking-api reads every branch on one clock (+06:00). customer-api puts group answers back on the salon's clock; single bookings still show +06:00. | Waits for the per-salon timezone decision (Olu). |
| M3 | booking-api ignores stylists' shifts when planning a party (it checks who is busy, not who is working). | Open, existing gap. |
| M4 | The app must send a product's `variant_id` (from `GET /salon/<id>/products`), not its `id`. | Tell the app team. booking-api's `unknown_product` message already says so. |
| M5 | Registered members see the party in `/bookings` too (decision D3). They cannot pay or cancel it. | As decided. |
| M6 | `docs/BOOKING_GROUP_API.md` §4 to §8 describe the old path. A banner says so; rewrite once `GROUP_BOOKING_V2` is on for good and the old path is removed. | Open. |

## Housekeeping

| # | What | Status |
| - | ---- | ------ |
| H1 | 18 tests fail locally in `apps.accounts` (login, OTP, registration) and `config` (logging), on the commit BEFORE the group work too. Not caused by it; probably the local `.env`. Worth checking in CI. | Open. |
| H2 | booking-api branch `feat/booking-products` mixed business and mobile commits. Split into `feat/calendar-filters` (business), `fix/tenancy-token` (all routes) and `feat/group-availability-many` (mobile). The old branch is left as it was. | Done locally, not pushed. |
| H3 | The parked `participantId` work is in `git stash` in both repos ("parked: ..."). The new path gives `members[].id` natively, so it can be dropped. | Waiting for Rafa's OK to drop. |
