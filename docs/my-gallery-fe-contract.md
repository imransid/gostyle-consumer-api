# My Gallery & Moments API

Base URL: https://api.gostyle.uk/api/v1 Auth: Bearer token on every call. A guest has no gallery.

Date: 2026-11-12. From: the mobile team.

My Gallery & Moments (src/app/(profile)/gallery-media.tsx, opened from the Profile tab) is where a customer keeps photos of cuts they like, to show a stylist at their next visit. The screen has:

- a black card with an Add Reference button, which opens a sheet with From Camera and From Album;
- filter chips: All, Haircut, Beard, Moments;
- the photos in two columns, each photo going to whichever column is shorter. A photo is square or tall.

Everything on it is demo data today. An added photo is kept in memory and is gone when the customer leaves the screen. The Profile tab's "24 items" under the row is hard coded too.

This doc asks for three calls. Photos are uploaded the way they already are, with POST /upload-files.

| Method | Endpoint | What for |
|---|---|---|
| GET | /me/gallery | Fills the screen, one page at a time. |
| POST | /me/gallery | Adds an uploaded photo as a reference. |
| DELETE | /me/gallery/{id} | Removes a reference. |

Ids, names and URLs in the examples are made up. Their shape is what we need.

## Rules for every part

- A gallery belongs to one customer. Only they can read it, add to it or remove from it. Nobody else (other customers, salons) reads it through these calls.
- References (haircut, beard) are added and removed by the customer.
- Moments are not added by hand: from Moments, the app's Add button adds a Haircut reference instead. What a Moment is still needs deciding. See [Open points](#7-open-points); the calls below work whatever the answer is.
- The server decides the categories and their labels, as it does for the rating tags. The app keeps no list of its own.
- Errors use the usual envelope (detail, code, errors[] with field, code, message), as in auth-error-response.md. The app branches on errors[i].code and shows message as it is.

## 1. GET /me/gallery

```
GET /me/gallery?category=haircut&page=1&pageSize=20
Authorization: Bearer <access token>
```

| Query | Type | Required | Default | Notes |
|---|---|---|---|---|
| category | string | no | (all) | One of the categories[].code. Left out: every category, for All. |
| page | int | no | 1 | As on GET /bookings. |
| pageSize | int | no | 20 | Capped at 50, as on GET /bookings. |

An unknown category is a 422 (see [Errors](#5-errors)), not an empty page.

### Where each part of the screen comes from

| On screen | Field |
|---|---|
| The chips after All, in this order | categories[].label, sent back as categories[].code |
| The photos | results[].thumbnail_url (or image_url) |
| Square or tall tile, before the photo loads | results[].width, results[].height |
| Remove (coming with this API) | results[].can_delete |
| Add Reference from the chip being looked at | categories[].can_add |
| Add Reference hidden when the gallery is full | limits.max_references, categories[].count |
| The item count under the row on the Profile tab | count of a page with no category |
| The empty message | count: 0 |

200 OK

```json
{
  "count": 37,
  "next": "https://api.gostyle.uk/api/v1/me/gallery?page=2&pageSize=20",
  "previous": null,
  "results": [
    {
      "id": "8f3c2a71-5e4d-4b19-a0c6-2d7e9b1f4a53",
      "category": "haircut",
      "image_url": "https://cdn.example.com/customers/gallery/8f3c2a71.jpg",
      "thumbnail_url": "https://cdn.example.com/customers/gallery/8f3c2a71-480.jpg",
      "width": 1080,
      "height": 1350,
      "can_delete": true,
      "created_at": "2026-11-12T09:41:07Z"
    },
    {
      "id": "b51e0d94-7c2a-4f36-9e81-6a3d0c5b7f28",
      "category": "beard",
      "image_url": "https://cdn.example.com/customers/gallery/b51e0d94.jpg",
      "thumbnail_url": null,
      "width": null,
      "height": null,
      "can_delete": true,
      "created_at": "2026-11-11T18:02:55Z"
    }
  ],
  "categories": [
    { "code": "haircut", "label": "Haircut", "count": 24, "can_add": true },
    { "code": "beard", "label": "Beard", "count": 10, "can_add": true },
    { "code": "moment", "label": "Moments", "count": 3, "can_add": false }
  ],
  "limits": {
    "max_references": 100
  }
}
```

count, next, previous and results page exactly like GET /bookings. count is the total for the category asked for, or for everything when none was asked for. categories and limits come on every page, whatever the category, so the chips never depend on the filter.

### results[]

| Field | Type | Null? | Notes |
|---|---|---|---|
| id | string (uuid) | never | The gallery item's own id, used by DELETE. Not the upload's id. |
| category | string | never | One of categories[].code. |
| image_url | string (full URL) | never | The photo at full size. |
| thumbnail_url | string (full URL) | can be null | A smaller copy, about 480 px wide, for the grid. null: the app loads image_url. Optional, but the grid holds dozens of photos of up to 10 MB each. |
| width | integer, pixels | can be null | The photo's real size, so the app can size the tile before the photo loads. null: the app draws it square. |
| height | integer, pixels | can be null | As width. Send both or neither. |
| can_delete | boolean | never | true for the customer's own references. false for anything they can't remove here (Moments, if they come from visits). |
| created_at | ISO 8601, UTC | never | When it was added. |

Order: newest first, so a photo just added shows at the top, as it does in the app today. With no category, every category is mixed in that one order.

Tile shape: the app keeps each tile between square and the design's tall tile (275 high for 173 wide). A wide photo is shown square and a very tall one as the tall tile, both cropped to fill, so the server never has to crop.

### categories[]

| Field | Type | Notes |
|---|---|---|
| code | string | Stable. Never renamed. What category takes and what POST sends. |
| label | string | Display text only. It can change at any time; the app never stores or compares it. |
| count | integer | How many items the customer has in it. 0 keeps the chip; the app shows the empty message. |
| can_add | boolean | Whether the customer can add to it. false for Moments if they come from visits. |

Order: the order the chips are drawn in, after the app's own All.

### limits

| Field | Type | Notes |
|---|---|---|
| max_references | integer | The most references one customer can keep (we suggest 100). Moments do not count. Once the counts of the can_add categories add up to it, the app hides Add Reference. |

## 2. Adding a reference

Two calls, the same way Rate your visit adds a photo: upload the file, then send its id.

### 2.1 POST /upload-files (exists, unchanged)

Multipart field files, one photo per request. It answers an array of { id, file_url, file_type }. As today, it needs a verified account and answers 403 permission_denied otherwise. The app only goes on when file_type is "IMAGE".

### 2.2 POST /me/gallery

```
POST /me/gallery
Authorization: Bearer <access token>
Content-Type: application/json
```

```json
{
  "file_id": "e8a3f1c6-47b2-4d90-8c5e-1b6f0a9d2e74",
  "category": "haircut"
}
```

| Field | Type | Required | Rules |
|---|---|---|---|
| file_id | string (uuid) | yes | An id from POST /upload-files, uploaded by this customer, file_type IMAGE, not already in their gallery. |
| category | string | yes | A code whose can_add is true. |

Which category the app sends: the chip being looked at, or haircut from All and from a chip that can't be added to. The design's sheet has no category picker.

201 Created: the new item, exactly as one row of results[].

```json
{
  "id": "8f3c2a71-5e4d-4b19-a0c6-2d7e9b1f4a53",
  "category": "haircut",
  "image_url": "https://cdn.example.com/customers/gallery/8f3c2a71.jpg",
  "thumbnail_url": "https://cdn.example.com/customers/gallery/8f3c2a71-480.jpg",
  "width": 1080,
  "height": 1350,
  "can_delete": true,
  "created_at": "2026-11-12T09:41:07Z"
}
```

thumbnail_url, width and height may still be null here if the server makes them after answering. The next GET should have them.

Sending the same file_id twice (a double tap, or a retry after a timeout) must not add it twice: answer 409 already_in_gallery with nothing saved, and the app treats it as done.

## 3. DELETE /me/gallery/{id}

```
DELETE /me/gallery/8f3c2a71-5e4d-4b19-a0c6-2d7e9b1f4a53
Authorization: Bearer <access token>
```

204 No Content, no body. Only for an item with can_delete: true.

The screen has no remove action yet. It will get one with this API, because without it a customer could never take a photo back out.

Deleting again an item already deleted answers 404 gallery_item_not_found. The app treats it as done.

## 4. Storage

- Keep, per item: the customer, the uploaded file, the category code, and when it was added. The file's size (width, height) is read once, when it is added.
- Removing a reference removes it from the gallery. Whether the file itself is deleted from storage is the server's choice; the app never uses its URL again.
- Deleting the account deletes the gallery and its files, like the rest of the customer's data.

## 5. Errors

| Call | Status | errors[0].code | field | When | App does |
|---|---|---|---|---|---|
| all | 401 | (auth codes) | | No token, or it has expired. | Refreshes the token and retries, or signs in. |
| GET | 422 | invalid_category | category | Not one of the codes. | App bug. Falls back to All. |
| POST | 422 | invalid_file | file_id | Unknown id, someone else's upload, or not an image. | Says the photo can't be used; asks for another. |
| POST | 422 | invalid_category | category | Not one of the codes, or a code with can_add: false. | Reloads the gallery (the categories changed). |
| POST | 409 | already_in_gallery | file_id | This file_id is already in the customer's gallery. | Treated as success; reloads the gallery. |
| POST | 409 | gallery_full | null | The customer has max_references references. | Shows message, e.g. "You can keep up to 100 photos. Remove one to add another." |
| DELETE | 404 | gallery_item_not_found | null | No such item, already deleted, or someone else's. The server never says which. | Treated as done; removes it from the screen. |
| DELETE | 409 | not_deletable | null | The item has can_delete: false. | App bug. Reloads the gallery. |

Nothing is saved when a call fails.

## 6. What the app does with it

So you can see why each field is asked for:

- Opens the screen with GET /me/gallery (no category), and draws the chips from categories. Each chip asks for its own pages, 20 at a time, loading more as the customer scrolls. A skeleton shaped like the grid shows while the first page loads.
- Draws each tile square or tall from width and height, and loads thumbnail_url, or image_url when there is none.
- Add Reference: picks the photo, uploads it with POST /upload-files, then sends POST /me/gallery, and puts the answer at the top of the grid. A 403 from the upload says to verify the account first.
- Remove: on an item with can_delete: true, asks once, then sends DELETE.
- Profile tab: "37 items" under My Gallery & Moments, from count.
- The black card's three photos become the customer's three newest, with the design's photos while there are fewer than three.

## 7. Open points

- What is a Moment? The app needs only the answer to know whether Moments can be added (can_add) and removed (can_delete). Our suggestion: the photos the customer attached to their own reviews (Rate your visit's photo_ids), shown here on their own, newest first, with can_add: false and can_delete: false (a review is final). Privacy already has a "Show my photo in Moments" switch (/me/privacy, show_photo_in_moments), which reads as if Moments are seen by other members. If so, that feed and who sees it needs its own doc; this one only lists the customer's own.
- Categories for ladies' salons. Is Beard right for every customer, or should the list depend on the customer (as beard_work is left out of a ladies' salon's rating tags)? Whatever is decided, the app just draws categories.
- Showing a reference to the stylist when booking is the point of the screen ("ready for your next visit"), but it is not in this doc. It would need the booking to carry gallery item ids, and the salon side to show them.
- Moving a photo between categories (PATCH /me/gallery/{id} with a new category) is not needed by the design today. Tell us if you would rather add it now.
