Service Details redesign: photos, menu group and what's included
Base URL: https://api.gostyle.uk/api/v1 Auth: Authorization: Bearer <access_token>, as today.

Date: 2026-10-30. From: the mobile team. For the Service Details screen (src/app/(salon)/service-details.tsx), redesigned in Figma 3908:16357 and 3908:17110. It builds on service-detail-screen.md and on the photo fields asked for in salon-services-packages.md.

0. What we are asking for
On GET /salon/:salon_id/service/:service_id, three new fields:

images: every photo of the service, in order.
group: the menu group the service sits in, after its category.
included_items: what the visit includes, each with a short line about it.
Nothing is removed and no field changes shape, so builds already in customers' hands keep working.

1. Why
The new screen is a large photo with a strip of thumbnails under it, then a path, the name, the description, the time and the price, then a "What's Included?" list:

On the screen	Needs	Today
Large photo and a scrolling strip	Every photo, in order	hero_url plus gallery, capped at 5
"Haircut & Styling > Precision Cuts"	The category, then the menu group	category only. The app borrows the group name from the profile
"Personal Consultation: Tailored style analysis based on face shape and hair type."	A name and a line for each item	included is names only, so the line is missing
"Add This Service"	Nothing new	The list lives in the app until booking
Until these fields arrive, the screen shows the demo photos the salon profile shows for that service, the group name the profile listed it under, and the included names without a line.

2. The response after the change
Only the three new fields are shown; every field in service-detail-screen.md stays.

{
  "id": "srv_gentlemans_cut",
  "salon_id": "sal_01j8xk2e9",
  "name": "The Gentleman's Cut",
  "description": "A classic haircut tailored to your preferences, including a wash, scalp massage, and styled finish.",
  "price": 199,
  "duration_min": 20,
  "duration_max": 30,
  "category": { "id": "haircut_styling", "label": "Haircut & Styling" },
  "group": { "id": "grp_precision_cuts", "name": "Precision Cuts" },
  "images": [
    "https://cdn.gostyle.uk/services/gentlemans-cut/1.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/2.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/3.jpg",
    "https://cdn.gostyle.uk/services/gentlemans-cut/4.jpg"
  ],
  "included": [
    "Personal Consultation",
    "Haircut & Precision Trimming",
    "Invigorating Shampoo & Scalp Massage",
    "Hot Towel Treatment",
    "Professional Styling"
  ],
  "included_items": [
    {
      "title": "Personal Consultation",
      "description": "Tailored style analysis based on face shape and hair type."
    },
    {
      "title": "Haircut & Precision Trimming",
      "description": "Custom classic cut executed with clippers and scissors."
    },
    {
      "title": "Invigorating Shampoo & Scalp Massage",
      "description": "Deep cleanse accompanied by a relaxing scalp massage."
    },
    {
      "title": "Hot Towel Treatment",
      "description": "A soothing steam towel to refresh the skin and scalp."
    },
    {
      "title": "Professional Styling",
      "description": "Finished with premium pomade, clay, or hair cream of your choice."
    }
  ]
}
Field	Type	Notes
images	array of URLs	Always present, [] when there are none. In the salon's order; the first is shown large. The same list as the service's images on GET /salon/:id/services
group	object or null	{ id, name }, the same ids as service_groups[].id on GET /salon/:id/services. null when the service is in no group
included_items	array	Always present, [] hides the block. In display order
included_items[].title	string	Short, shown in bold, followed by a colon
included_items[].description	string or null	One sentence. null shows the title alone
images
No cap of 5. The strip under the large photo scrolls, so send every photo, up to 10.
Shape: the large photo is 350 × 420 on screen (portrait, 5:6), and the thumbnails crop the same file to a square. Please serve at least 700 × 840 (2×), JPEG or WebP, ideally under 200 KB each. The app crops to fill, centred.
hero_url, gallery and gallery_count stay for older builds. When images is sent, the new screen reads only images.
included_items
Why a new field, not a new shape for included: builds already installed draw included as text, and an object there would crash them. Please keep included as the titles alone.
The app adds the colon itself, so do not end title with one.
3. What the screen no longer reads
rating, review_count, details, preparation, products and experts are not on the new design. Keep sending them while older builds are in use; we will tell you when they can go.

4. Errors
Unchanged from service-detail-screen.md: 404 when the service is not on this salon's menu, 401 when the token has expired.

5. What the app does with them
Photos: images[0] large, all of images in the strip; tapping a thumbnail shows it large. With images missing it falls back to hero_url and gallery, then to demo photos.
Path: category.label, a chevron, then group.name. With no group, the category alone.
What's Included?: each item as "Title: description". Without included_items, the titles from included alone.
"Add This Service": ticks the service on the salon profile and shows "Service Added to your list!". No call is made; the booking flow sends the picked services as it does today. Hidden when is_active is false.
6. Open points
Where the lines come from: can a salon write the line for each included item today, or does the partner app need a field for it?
Photos: is 10 the right limit, or do salons upload more?
