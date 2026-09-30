Service Detail Screen
Base URL: https://api.gostyle.uk/api/v1 Auth: Bearer token on every request.

The screen a customer opens from a salon's Services tab: the hero, the price and duration, the gallery, what the visit includes, how to prepare, the products the salon uses, and the experts who can do it. Picking an expert is what enables Book Appointment, so the same ids this call answers are the ones the booking flow then sends.

Only one new endpoint is needed. GET /services-details (see service-details.md) stays as it is: it resolves several ids into their basic fields for the review screen, and knows nothing about galleries or experts.

1. Endpoint
Method	Path	Purpose
GET	/salon/{salon_id}/service/{service_id}	Everything this screen draws
GET /salon/c6c248ab-f2cd-4f12-a31e-243c6e64b3b5/service/bea5b243-8447-4f41-beff-14777f930ee7
Both ids are in the path because a service only exists inside a salon: the same haircut at another branch is another id, at another price.

Reused, unchanged:

Method	Path	Used by
GET	/salon/:id/stylists?service_ids=	The same experts, when the list is opened
GET	/salon/:id/products	The Shop tab, for a product's variant id
2. Response — 200 OK
{
  "id": "bea5b243-8447-4f41-beff-14777f930ee7",
  "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
  "name": "The Gentleman's Cut",
  "description": "A classic haircut tailored to your preferences, including a wash, scalp massage, and styled finish.",
  "price": 180,
  "duration_min": 20,
  "duration_max": 30,
  "category": { "id": "haircut_styling", "label": "Haircut & Styling" },
  "rating": 4.8,
  "review_count": 129,
  "is_active": true,
  "hero_url": "https://cdn.gostyle.uk/services/gentlemans-cut/hero.jpg",
  "gallery": [
    "https://cdn.gostyle.uk/services/gentlemans-cut/1.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/2.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/3.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/4.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/5.jpg"
  ],
  "gallery_count": 17,
  "included": ["Consultation", "Scalp Massage", "Wash", "Hair cut", "Styling"],
  "details": [
    { "label": "Time Duration", "value": "20-30 min", "icon": "clock" },
    {
      "label": "Suitability",
      "value": "Suitable for all hair types, especially medium-to-long hair.",
      "icon": "scissors"
    }
  ],
  "preparation": [
    "Arriving with clean hair is recommended.",
    "Ask your stylist for product recommendations to maintain your style at home."
  ],
  "products": [
    {
      "id": "cccccccc-cccc-cccc-cccc-cccccccccc01",
      "variant_id": "dddddddd-dddd-dddd-dddd-dddddddddd01",
      "name": "Volume Shampoo",
      "price": 240,
      "image_url": "https://cdn.gostyle.uk/products/volume-shampoo.png"
    },
    {
      "id": "cccccccc-cccc-cccc-cccc-cccccccccc02",
      "variant_id": "dddddddd-dddd-dddd-dddd-dddddddddd02",
      "name": "Beard Balm",
      "price": 320,
      "image_url": "https://cdn.gostyle.uk/products/beard-balm.png"
    }
  ],
  "experts": [
    {
      "id": "04e58d74-04db-4088-bd97-e3ff765cc322",
      "name": "Darius Stone",
      "title": "Hair Stylist",
      "role": "Haircut & Styling Expert",
      "rating": 4.8,
      "review_count": 320,
      "avatar_url": "https://cdn.gostyle.uk/stylists/darius.png"
    },
    {
      "id": "45bd1d8e-0699-4b04-8172-07a65cb4e858",
      "name": "Liam Johnson",
      "title": "Barber",
      "role": "Barber & Grooming",
      "rating": 4.7,
      "review_count": 210,
      "avatar_url": null
    }
  ]
}
Fields
Field	Type	Notes
id	string	The service id. Goes to the booking flow as serviceIds.
salon_id	string	Echoed back so the screen can open the booking flow without the route param.
name	string	The title.
description	string	One paragraph under the title. null when the salon wrote none.
price	number	One visit, before VAT, in the salon's currency.
duration_min	number	Minutes. The screen prints "20-30 min" from the pair — see §3.
duration_max	number	Equal to duration_min when the visit is a fixed length.
category	object	{ id, label }, the same ids /salon/:id/services groups by.
rating	number	0 to 5, one decimal. null when nobody has rated it.
review_count	number	0 when there are none; the screen then shows "No reviews yet".
is_active	boolean	false hides the Book button — see §3.
hero_url	string	The header image. null falls back to the salon's cover.
gallery	string[]	At most 5, in display order: four thumbs and the tile behind "+N".
gallery_count	number	How many photos exist in all. "+N" is gallery_count - gallery.length.
included	string[]	The chips under "What's Included". [] hides the block.
details	array	The "Key Details" rows, in order.
↳ label	string	Row label, e.g. "Suitability".
↳ value	string	Row text, already written out for display.
↳ icon	string	A hint the app maps to its own icon: clock, scissors, sparkles, drop. Unknown values fall back to a dot.
preparation	string[]	"Preparation & Aftercare" bullets. [] hides the block.
products	array	"Products Used". [] hides the block.
↳ id	string	The product id.
↳ variant_id	string	What a booking sends as the product's id — as in booking-create.md.
↳ price	number	Unit price, before VAT.
↳ image_url	string	null renders the grey tile with no photo.
experts	array	Who can do this service, best first. [] means the salon assigns.
↳ id	string	The stylist id the booking flow sends.
↳ title/role	string	Printed as one line; either can be null.
↳ avatar_url	string	null renders the initials circle.
3. Rules the backend must follow
One call draws the whole screen. No follow-up per section: the customer opens the screen once and everything below the fold is already there.
The gallery is capped at five. The app shows four thumbs and a fifth behind "+N", so sending more wastes bytes. gallery_count carries the real number; when it is 5 or fewer the app shows no "+N".
experts is the same list as /salon/:id/stylists?service_ids={id}, in the same order and the same shape. It is embedded so the screen does not need a second call; the endpoint stays for the booking flow's Expert step.
Prices are the salon's own, before VAT, matching the Services tab to the fil. A price shown here and a price in the booking payload must agree, or the create fails with amount_mismatch.
Duration is a pair, never a sentence. duration_min and duration_max are minutes; the app writes "20 mins" when they are equal and "20-30 mins" when they are not. details[].value may still spell it out for the Key Details row.
is_active: false is still readable. A service pulled from sale answers 200 with its details so a deep link or an old booking still opens, but the app hides Book Appointment.
Only the salon's own rows. A service_id that belongs to another salon is 404, never a cross-salon read.
4. Errors
Same envelope as auth-error-response.md.

Case	Status	code
No such salon	404	not_found
No such service, or not at this salon	404	not_found
An id in the path is not a UUID	404	not_found
Missing or expired token	401	—
{
  "detail": "This service is no longer on the menu.",
  "code": "not_found",
  "errors": []
}
The app shows detail on an empty state with a "Back to salon" button, so the sentence is worth writing for a customer rather than for a log.

5. What the screen does next
Picking an expert enables Book Appointment, which opens the booking flow with what this call answered:

Screen state	Passed on as
salon_id	salonId — the salon every later call is scoped to
id	serviceIds (a list of one)
The chosen expert	The Expert step's pre-selection; null means any expert
Ticked products	Carried to Review, sent as variant_id in the booking payload
Nothing on this screen is booked or held: it is a read, and the booking flow re-checks prices, stylists and times when it creates the booking (booking-create.md §3).

6. Open points
Reviews are a count, not a list. The screen shows "129 Reviews" but has nowhere to read them; a GET /salon/:id/service/:id/reviews would fill the ratings screen when that is built.
The gallery has no ids. Tapping "+N" opens the stories viewer, which takes URLs only. If a photo ever needs a caption or an author, gallery becomes a list of objects.
details[].icon is a loose string. It is a hint, not a contract; the app falls back safely. Worth turning into an enum once the set settles.
