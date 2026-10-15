Package Details Screen
Base URL: https://api.gostyle.uk/api/v1 Auth: Bearer token on every request.

Date: 2026-10-15. From: the mobile team.

The screen a customer opens from a salon's Packages tab by tapping a bundle: the hero with the price and what it saves, what the package includes, the visit timeline, how it works, the saving card, the terms line, and Book with this Package.

This doc describes the screen as it is built today, in src/app/(salon)/package-details.tsx. Every value on it is hardcoded: the screen makes no call of its own. It is opened with the package id alone, from the list call GET /salon/{id}/packages, whose row carries the name, the prices and a duration, and nothing else the screen draws.

One new endpoint. The list call stays as it is.

1. Endpoint
Method	Path	Purpose
GET	/salon/{salon_id}/package/{package_id}	Everything this screen draws
GET /salon/c6c248ab-f2cd-4f12-a31e-243c6e64b3b5/package/9f1c7a2e-5b84-4a3d-9e60-71c2d8f4a013
Both ids are in the path for the same reason as a service: a package only exists inside a salon, and the same bundle at another branch is another id at another price. The app opens this screen with the package id alone today, and will pass the salon id with it.

Reused, unchanged:

Method	Path	Used by
GET	/salon/:id/packages	The Packages tab, which opens this screen
2. Response: 200 OK
Every field below is drawn on the screen. Nothing here is for later.

{
  "id": "9f1c7a2e-5b84-4a3d-9e60-71c2d8f4a013",
  "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
  "name": "The Full Groom",
  "description": "The complete reset cut, beard, hot towel.",
  "price": 460,
  "price_before": 540,
  "save_amount": 103,
  "save_percent": 15,
  "duration_minutes": 75,
  "is_active": true,
  "includes": [
    {
      "service_id": "bea5b243-8447-4f41-beff-14777f930ee7",
      "name": "The Gentleman's Cut",
      "price": 199
    },
    {
      "service_id": "1c0d7f58-9a41-4c2b-8de3-6f5b0a2c9d47",
      "name": "Beard Trim & Shave",
      "price": 230
    },
    {
      "service_id": "7e34b9a0-2d61-4f8c-a5b7-c0e819d3f462",
      "name": "Hot Towel Shave",
      "price": 260
    }
  ],
  "timeline": [
    {
      "service_id": "bea5b243-8447-4f41-beff-14777f930ee7",
      "name": "The Gentleman's Cut",
      "offset_minutes": 0,
      "duration_minutes": 75
    },
    {
      "service_id": "1c0d7f58-9a41-4c2b-8de3-6f5b0a2c9d47",
      "name": "Beard Trim & Shape",
      "offset_minutes": 20,
      "duration_minutes": 30
    },
    {
      "service_id": "7e34b9a0-2d61-4f8c-a5b7-c0e819d3f462",
      "name": "Hot Towel Shave",
      "offset_minutes": 50,
      "duration_minutes": 25
    }
  ],
  "terms": "Valid 90 days from activation, any stylist at The Iron Razor, sessions deduct at booking confirmation, unused sessions refund to wallet if you cancel the package."
}
Fields
Field	Type	Where it lands on the screen
name	string	The hero title
description	string | null	The line under it. Null hides the line
price	number	The big price beside the dirham mark
price_before	number | null	The struck-through price. Null hides it and the SAVE chip
save_percent	number	The "SAVE 15%" chip, and the "(15%)" in the saving card
save_amount	number	The saving card's "103"
duration_minutes	number	"Time: ~75 mins" in the hero
is_active	boolean	False hides Book with this Package
includes[].name	string	The left of each What's Included row
includes[].price	number	The right of it, beside the dirham mark
includes[].service_id	uuid	Not drawn. What the button preselects (ยง5)
timeline[].offset_minutes	number	The left column: 0 prints "Start", the rest print "+20 min"
timeline[].name	string	The step's title
timeline[].duration_minutes	number	"~30 mins" under it
terms	string	The grey footnote at the bottom. Plain text, one paragraph
Minutes, not sentences. The screen holds strings today ("+20 min", "~75 mins"). Send numbers and the app will write them, so one package reads the same as the next and the words can be translated later.

Not needed from the API. The three How it works steps ("Buy and activate instantly", "Book any included service", "Check in, covered") are the same for every package at every salon, so the app keeps them. Tell us if they should ever differ per package and we will take them from here instead. The saving card's "Pays for itself in one visit" is the app's own line too.

3. Rules the backend must follow
save_amount is not the price difference. The design has 540 down to 460, which is 80, but the saving card says 103: the saving is counted against what the services cost separately (199 + 230 + 260 = 689). The app cannot work that out, so send it. Send save_percent as a whole number, never the string "15%".
price_before and the SAVE chip travel together. No price_before means no chip and no struck-through price, and the saving card is hidden.
timeline[].offset_minutes counts from the start of the visit, in minutes, in order, the first one 0.
The timeline and the included list are two different things. The design has three included services and three timeline steps, but their durations do not add up to the hero's 75 minutes, and one is named "Beard Trim & Shave" in one list and "Beard Trim & Shape" in the other. Whatever is right, send each list in full rather than expecting the app to pair them up.
Every service_id must be bookable at that salon. They are what the booking flow preselects, so a package holding a retired service breaks the button.
Plain text only. description and terms are printed as they come, no markup, and terms is one paragraph, not a list.
Money is a number in the salon's own currency, as everywhere else. The dirham mark is drawn by the app.
4. Errors
The usual envelope:

Status	code	When	The app
401	not_authenticated	No token, or it expired	Refreshes and tries again
404	not_found	No such package, or not at that salon	"We couldn't load this package"
503	service_unavailable	The service is down	Offers another try
{
  "detail": "Please correct the highlighted fields.",
  "code": "not_found",
  "errors": [
    { "field": "id", "code": "not_found", "message": "No such package." }
  ]
}
A guest with no account may read this screen. Only the button behind it asks them to sign in.

5. Book with this Package
The button opens the package booking screen, which is hardcoded too and is handed nothing today. For a session to be deducted when that booking is made, booking create has to know which package paid for it:

{
  "salon_id": "c6c248ab-...",
  "package_id": "9f1c7a2e-...",
  "services": [{ "service_id": "bea5b243-...", "...": "..." }]
}
Please confirm what booking create expects, whether the package is bought at that moment or beforehand, and what it answers when the package is sold out or no longer active. booking-create.md says nothing about packages today, and the app sends nothing.

6. What the screen does next
Opened from the Packages tab with the salon id and the package id.
Reads this endpoint once. There is nothing to poll and nothing to write.
Draws the hero, the three cards, the saving card and the terms line.
Book with this Package asks a guest to sign in, then goes on to the package booking screen with the ids from includes[].
7. Open points
Is there a second state for a package the customer already owns? The Packages tab has a "My Packages" list with sessions used and an expiry, and tapping one opens this same screen, which knows nothing about any of it. If this read should carry the customer's own copy (sessions used, expires on, usage history), say so and we will write that up separately.
Should the hero follow the list's theme? The list rows are coloured by theme; this screen's hero is always black. Send it and we will match, or leave it and the hero stays as it is.
The Share button does nothing yet. If a package should be shareable we need a link for it, the way a salon has one.
duration_minutes in the hero reads "~75 mins". Is that the chair time for one visit, or the whole package? The timeline's first step is also 75, which makes the two look like the same number by accident.
