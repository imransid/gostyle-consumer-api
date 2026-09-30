Expert Profile Screen
Base URL: https://api.gostyle.uk/api/v1 Auth: Bearer token on every request.

One stylist at one salon: the story strip at the top, who they are, how they rate, the work they have posted, and the services they personally do. Ticking services and tapping Continue opens the booking flow with that stylist already chosen, so the ids here are the ids the booking then sends.

Only one new endpoint is needed. GET /salon/:id/stylists stays as it is — it lists the team for a salon and for the booking flow's Expert step, and knows nothing about media or grouped services.

1. Endpoint
Method	Path	Purpose
GET	/salon/{salon_id}/stylist/{stylist_id}	Everything this screen draws
GET /salon/c6c248ab-f2cd-4f12-a31e-243c6e64b3b5/stylist/04e58d74-04db-4088-bd97-e3ff765cc322
Both ids are in the path: a stylist is read in the context of the salon whose prices, hours and services the screen shows.

Reused, unchanged:

Method	Path	Used by
GET	/salon/:id/stylists?service_ids=	The team list, and the booking Expert step
POST	/favourite	The heart — see §5
2. Response — 200 OK
{
  "id": "04e58d74-04db-4088-bd97-e3ff765cc322",
  "salon_id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
  "name": "Darius Stone",
  "title": "Master Barber",
  "role": "Haircut & Styling Expert",
  "bio": "Darius Stone is a master barber with over 15 years of experience. He specializes in modern cuts, classic styles, and beard grooming.",
  "avatar_url": "https://cdn.gostyle.uk/stylists/darius.png",
  "rating": 4.8,
  "review_count": 320,
  "years_experience": 15,
  "price_level": 3,
  "day_off": "Tuesday",
  "is_network_member": true,
  "is_favorite": false,
  "has_story": true,
  "media": [
    {
      "id": "8e2b1f10-0000-4000-8000-000000000001",
      "type": "image",
      "url": "https://cdn.gostyle.uk/stylists/darius/1.jpg",
      "thumbnail_url": null
    },
    {
      "id": "8e2b1f10-0000-4000-8000-000000000002",
      "type": "video",
      "url": "https://cdn.gostyle.uk/stylists/darius/reel.mp4",
      "thumbnail_url": "https://cdn.gostyle.uk/stylists/darius/reel.jpg"
    }
  ],
  "media_count": 29,
  "salon": {
    "id": "c6c248ab-f2cd-4f12-a31e-243c6e64b3b5",
    "name": "Green Wave Salon",
    "is_open": true,
    "hours_today": "10:00 AM - 9:00 PM",
    "latitude": 23.808937,
    "longitude": 90.417724
  },
  "service_groups": [
    {
      "id": "grp_precision_cuts",
      "name": "Precision Cuts",
      "services": [
        {
          "id": "bea5b243-8447-4f41-beff-14777f930ee7",
          "name": "The Gentleman's Cut",
          "description": "A classic haircut tailored to your preferences, including a wash, scalp massage, and styled finish.",
          "price": 199,
          "duration_min": 20,
          "duration_max": 30
        },
        {
          "id": "bea5b243-8447-4f41-beff-14777f930ee8",
          "name": "Beard Trim & Shape",
          "description": "Expert shaping and trimming to achieve your desired beard style.",
          "price": 230,
          "duration_min": 30,
          "duration_max": 40
        }
      ]
    },
    {
      "id": "grp_styling",
      "name": "Styling & Grooming",
      "services": [
        {
          "id": "bea5b243-8447-4f41-beff-14777f930ee9",
          "name": "Hair Styling",
          "description": "A professional styling session with blow-dry and finishing.",
          "price": 245,
          "duration_min": 10,
          "duration_max": 20
        }
      ]
    }
  ]
}
Fields
Field	Type	Notes
id	string	The stylist id. Goes to the booking flow as the chosen expert.
salon_id	string	Echoed back, so the screen opens the booking flow without the route param.
name	string	The title line.
title/role	string	"Master Barber" and the speciality. Either can be null; the screen prints whichever it has.
bio	string	One paragraph. null hides the block.
avatar_url	string	The ringed avatar. null renders initials.
rating	number	0 to 5, one decimal. null when nobody has rated them.
review_count	number	0 shows "No reviews yet" instead of the count.
years_experience	number	null when the salon did not fill it in.
price_level	number	1 to 4, drawn as $ to $$$$. null hides the row.
day_off	string	Full day name, shown as it is. null hides the row.
is_network_member	boolean	Draws the "Network Member" badge.
is_favorite	boolean	Fills the heart — see §5.
has_story	boolean	Whether tapping the avatar opens a story.
media	array	Their posted work, newest first. At most 5 — see §3.
↳ type	enum	image or video. A video tile draws the play button.
↳ url	string	The image, or the video file.
↳ thumbnail_url	string	Required for a video, null for an image.
media_count	number	How many exist in all; the last tile shows +media_count - media.length.
salon	object	The salon this stylist is being read at.
↳ is_open	boolean	Drives the green "Open" / red "Closed".
↳ hours_today	string	Printed as it is, e.g. "10:00 AM - 9:00 PM". null hides it.
↳ latitude/longitude	number	The app works out the "3.1km" itself from the phone's position — see §3.
service_groups	array	Only what this stylist does, grouped and ordered as the Services tab.
↳ services[]	array	The same fields as /salon/:id/services, so one row renderer serves both.
3. Rules the backend must follow
One call draws the whole screen. No follow-up for media, services or the salon's hours.
media is capped at five. The hero story strip shows them in order and the grid draws one video, three photos and a "+N" tile, so more is wasted bytes. media_count carries the real number; when it is 5 or fewer the app shows no "+N".
Distance is the app's job. Send the salon's coordinates; the phone knows where it is and the server does not need to.
Services are this stylist's own, filtered to what they are skilled in — the same filter /salon/:id/stylists?service_ids= applies in reverse. A service the salon sells but this stylist cannot do must not appear.
Prices are the salon's prices for those services, to the fil. The booking re-checks them (booking-create.md §3), so a different number here only produces amount_mismatch later.
Durations are a pair of minutes, never a sentence: the app writes "20 mins" or "20-30 mins" from duration_min and duration_max.
A stylist who no longer works there is 404. So is one who works at another salon, even with a real id — never a cross-salon read.
is_favorite is per customer, from the Bearer token, and false when signed out.
4. What the screen does next
Ticking services enables Continue (N selected), which opens the booking flow already past the Expert step:

Screen state	Passed on as
salon_id	salonId
The ticked ids	serviceIds
id	The chosen stylist — the flow opens on the Time step
The flow then calls GET /booking/nearest-available/{salon_id} with stylist_id set, exactly as if the stylist had been picked in the Expert step.

5. The heart
The heart reuses the favourites endpoint rather than adding one:

POST /favourite
{ "stylist_id": "04e58d74-04db-4088-bd97-e3ff765cc322" }
The existing body ({ "salon_id": "..." }) is unchanged; exactly one of the two ids is sent.
It toggles: the answer carries the new is_favorite, and the screen draws what comes back rather than guessing.
Signed out, it is 401 and the app sends the customer to sign in.
6. Errors
Same envelope as auth-error-response.md.

Case	Status	code
No such salon	404	not_found
No such stylist, or not at this salon	404	not_found
An id in the path is not a UUID	404	not_found
Missing or expired token	401	—
{
  "detail": "This stylist is no longer at this salon.",
  "code": "not_found",
  "errors": []
}
The app shows detail on an empty state with a "Back to salon" button, so the sentence is written for a customer, not for a log.

7. Open points
Reviews are a count, not a list. "320 reviews" has nowhere to go until a GET /salon/:id/stylist/:id/reviews exists for the ratings screen.
Stories and media are two ideas. The avatar ring opens the story viewer while the grid shows posted work; today both would read media. If stories ever expire on their own, they need their own list.
price_level is the stylist's, not the salon's. If it always mirrors the salon it can be dropped and read from the profile instead.
Per-stylist pricing is not modelled: a senior stylist charging more than the salon's list price would need a price on the stylist's service row.
