# Privacy & Visibility (FE Integration Guide)

> **Live on production. Build from `docs/NOTIFICATIONS_FE_GUIDE.md`**, the
> one guide for the whole feature. This page keeps the server steps.

Date: 2026-10-31. For: the mobile app team. The Privacy & Visibility
screen. Server steps at the end.

Base URL: `https://api.gostyle.uk/api/v1`
Auth: `Authorization: Bearer <access_token>`.

---

## 0. Read this first

1. **Behind a server switch, `PRIVACY_SETTINGS_V1`.** While it is off, both
   calls answer `404`. We tell you when it is on.
2. **Four rows on the screen, two settings here:**

   | Row | Where it lives |
   | --- | --- |
   | Show my name to salons | `show_name_to_salons` (this API) |
   | Show my photo in Moments | `show_photo_in_moments` (this API) |
   | Download my data | its own calls (coming, a separate guide) |
   | Location access | the phone's own permission. The server keeps nothing for it. |

3. **Show my name to salons, off:** salons see your first name and the
   initial of your last name, "Sara A.", enough for the desk to call you.
   It applies at once to:
   - the salon's record of you that the app made when you first booked,
   - the name at the check-in desk,
   - your name on a group booking's lane.

   A salon's own record of you from a visit before the app is the salon's,
   and stays as the salon wrote it. Say so near the switch, for example:
   "Salons that knew you before GoStyle keep their own records."
4. **Show my photo in Moments** is kept for Moments, which is not built
   yet: it changes nothing today. Off by default.
5. Validation errors are `422`, as everywhere in this API.

## 1. GET /me/privacy

```json
{"show_name_to_salons": true, "show_photo_in_moments": false}
```

## 2. PATCH /me/privacy

Partial: send only what changed. All of it is saved, or none.

```http
PATCH /me/privacy
Content-Type: application/json

{"show_name_to_salons": false}
```

`200` with both settings:

```json
{"show_name_to_salons": false, "show_photo_in_moments": false}
```

## 3. Errors

| When | Status | `code` (`errors[0].code`) |
| --- | --- | --- |
| No token | `401` | `not_authenticated` |
| Switch off | `404` | `not_found` |
| Not a JSON `true`/`false` | `422` | `validation_error` (`invalid`) |
| A key we do not know | `422` | `validation_error` (`unknown_field`) |
| The salon's record could not be updated (nothing was saved; try again) | `503` | `service_unavailable` |

---

## Server steps

One PR, `feat/privacy-settings`, base `main`. Migration `accounts/0029`:
two columns on `consumer_account`, `show_name_to_salons` (`DEFAULT true`)
and `show_photo_in_moments` (`DEFAULT false`), no backfill. An older image
runs fine on top of it.

```sh
docker tag gostyle-consumer-api:local gostyle-consumer-api:before-privacy
cd ~/gostyle-customer && git pull && git log --oneline -1
docker build -t gostyle-consumer-api:local .
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_api
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_grpc
docker service update --force --image gostyle-consumer-api:local gostyle-consumer_worker
```

`_grpc` matters here: it gives booking-api the name for the check-in desk.

Turn it on:

```sh
docker service update --env-add PRIVACY_SETTINGS_V1=true gostyle-consumer_api
grep -q '^PRIVACY_SETTINGS_V1=' /root/gostyle-customer/stack.env || echo 'PRIVACY_SETTINGS_V1=true' >> /root/gostyle-customer/stack.env
```

Roll back: the switch off (`--env-rm PRIVACY_SETTINGS_V1`) stops changes;
a customer who already turned the name off stays shortened until the
whole release is rolled back (the image from the first line). The columns
stay; the older image does not read them.
