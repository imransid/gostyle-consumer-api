# Service area alerts: FE guide

For the mobile team and QA. Server switch: `SERVICE_AREA_ALERTS_V1` (off = both routes answer 404).

## 1. Why no alert ever arrived before

The radii in Admin Panel -> Build -> Service Area were saved, but nothing used them:

- the app had no endpoint to send its location to (none existed in any GoStyle service);
- no backend compared a location with a salon's radii;
- the values never left the build wizard's saved step.

So no code ran and nothing was logged. This guide is the new contract. **The app must send its location**; the server decides everything else.

## 2. What the customer gets

Only for a customer with a **confirmed visit** (`CONFIRMED_BY_SALON`) that **starts within 3 hours and has not ended**. Each ring is measured from the salon's map pin.

| Ring (admin panel) | Example | Push `type` | Title / body |
|---|---|---|---|
| Primary Appointment Radius | 800 m | `visit.nearby` | "You're near Demo Salon & Spa" / "Your booking GS-1500 is today at 3:45 PM." Only before the start time. |
| Auto-Trigger Distance | 200 m | `visit.almost_there` | "Almost there" / "You're a short walk from Demo Salon & Spa. Tap to check in when you walk in." (or the booking time when check-in is not open yet) |
| Arrival Radius | 50 m | `visit.arrived` | "Welcome to Demo Salon & Spa" / "Tap to check in so the desk knows you're here." (or "Show booking GS-1500 at the desk.") |
| Home-visit Radius | 15 km | none | Travel range for home visits. It is not an alert ring. |

Rules:

- **Once per ring per salon per day.** Leaving and coming back sends nothing new.
- **The innermost ring wins.** A first reading at the door sends only `visit.arrived`, and the outer rings are passed silently. A wobbly fix after that sends no "near" alert.
- **Exactly on the line counts as inside.** To count as gone, the customer must be 10% of the ring (at least 10 m) past it.
- **A reading whose `accuracy_m` is wider than a ring changes nothing for that ring.** An 80 m indoor fix cannot place anyone in a 50 m ring.
- **Same switches as reminders.** "Appointment reminder" off, or push off: the item still goes into the Notification Center (Updates tab), but no push is sent.
- **A push is dropped if it is not out within 10 minutes**, and never sent after the visit ends.
- **No marketing alerts.** Being near a salon with no visit there sends nothing.

## 3. POST /api/v1/me/location

Send one reading. Bearer token as everywhere.

```json
{
  "latitude": 25.2048,
  "longitude": 55.2708,
  "accuracy_m": 12,
  "recorded_at": "2026-11-12T11:41:07Z"
}
```

| Field | Rule |
|---|---|
| `latitude` | required, -90..90 |
| `longitude` | required, -180..180 |
| `accuracy_m` | optional, metres, 0..10000. Send it: it stops blurred fixes from firing. |
| `recorded_at` | optional ISO 8601 with offset (the fix time). Older than 5 minutes: not measured. More than 2 minutes in the future: 422. |

**200**

```json
{
  "ignored": null,
  "visits": [
    {
      "booking_id": "0b8f6c1e-3a52-4d7e-9c41-5f2a7e9d8b10",
      "salon_id": "7c2d9a4e-1b3f-4e6a-8d5c-9f0e1a2b3c4d",
      "distance_m": 180,
      "skipped": null,
      "zones": [
        {"zone": "arrival", "radius_m": 50, "inside": false, "notified": false},
        {"zone": "auto_trigger", "radius_m": 200, "inside": true, "notified": true},
        {"zone": "primary", "radius_m": 800, "inside": true, "notified": true}
      ],
      "sent": ["visit.almost_there"]
    }
  ]
}
```

- `visits: []` means there is no visit to watch right now (this is normal).
- `ignored: "stale"` means the reading was older than 5 minutes.
- `skipped` is `salon_not_found`, `no_coordinates` (salon has no map pin) or `no_service_area` (salon set no usable ring). These are salon setup problems, not app problems.
- `sent` is what this reading sent. The push itself arrives a second or two later, from the worker.

The app does not need to act on the answer. It is there for QA and debugging.

**Errors** (the usual envelope): 401; 404 (switch off); 422 with `required`, `invalid_latitude`, `invalid_longitude`, `invalid_accuracy` or `invalid_recorded_at` on the field; 429 over 20 readings a minute; 503 when visits cannot be read (just send the next reading).

The server never stores the coordinates. It keeps only, per ring, inside or not and the distance to the salon.

## 4. GET /api/v1/me/geofences

The rings of the customer's confirmed visits in the next 24 hours, for the phone's own geofencing. Innermost first within a visit.

```json
{
  "regions": [
    {
      "id": "0b8f6c1e-3a52-4d7e-9c41-5f2a7e9d8b10:arrival",
      "booking_id": "0b8f6c1e-3a52-4d7e-9c41-5f2a7e9d8b10",
      "salon_id": "7c2d9a4e-1b3f-4e6a-8d5c-9f0e1a2b3c4d",
      "zone": "arrival",
      "latitude": 25.2048,
      "longitude": 55.2708,
      "radius_m": 50,
      "starts_at": "2026-11-12T11:45:00Z",
      "ends_at": "2026-11-12T12:45:00Z"
    }
  ]
}
```

## 5. When the app sends a reading

1. **On app open / foreground**, if the customer has a visit today: one reading.
2. **Background, battery friendly (recommended):** after sign-in, on app open, and after booking or cancelling, call `GET /me/geofences` and register each region with the OS. Use iOS `CLLocationManager.startMonitoring(for: CLCircularRegion)` or Android `GeofencingClient`. Replace the old set each time. iOS allows 20 regions and Android 100. On an **enter** event, get one fresh fix and `POST /me/location`. Drop regions after `ends_at`.
3. **While the app is open** and a visit starts within 3 hours: one reading on a significant move (about 50 m), or every 1 to 2 minutes, whichever comes first. Never faster than every 3 seconds.

Permissions: background geofencing needs iOS "Always" location and Android `ACCESS_BACKGROUND_LOCATION`. With "While using" only, step 1 and step 3 still work, but alerts come only while the app is open.

## 6. The push

The `data` keys (all strings), as for the other Notification Center pushes:

| Key | Value |
|---|---|
| `type` | `visit.nearby`, `visit.almost_there` or `visit.arrived` |
| `category` | `updates` |
| `notificationId` | the Notification Center item |
| `targetType` / `targetId` | `booking` / the booking id |
| `bookingId`, `bookingCode`, `startAt`, `salonId` | the visit |
| `zone` | `primary`, `auto_trigger` or `arrival` |
| `canCheckIn` | `"true"` when "I am here" is open now (same rule as `can_check_in` on GET /bookings) |

On tap, open the booking (`GET /booking/{bookingId}`). When `canCheckIn` is `"true"`, the screen's check-in button is live.

An older app build that does not know these types still shows the push (FCM `notification` block). It just opens the app's default screen.

## 7. QA: how to reproduce the original bug report

1. On the salon's build, confirm Service Area: Primary 800 m, Auto-Trigger 200 m, Arrival 50 m. Confirm the salon has a map pin (Location step lat/lng).
2. With the test customer, **book a visit at that salon starting within 3 hours**, and have the desk confirm it (status Confirmed by salon).
3. On the phone, allow notifications and location. Make sure Settings -> Notifications -> Appointment reminders is on.
4. Simulate a location 500 m from the salon. Expect the "You're near ..." push, and `sent: ["visit.nearby"]` if you call the API by hand.
5. Simulate 150 m: expect "Almost there". Then 20 m: expect "Welcome to ...".
6. Repeat step 4: no new push (once per ring per day).
7. A customer **with no visit** at the salon gets no push. That is by design (section 2).

## 8. Server

- Migration: `geofence 0001` (new table `geofence_state`).
- Switch: `SERVICE_AREA_ALERTS_V1=true` on the api service (and in `stack.env`). The worker already sends the pushes.
- Optional: `LOCATION_PING_RATE` (default `20/min`).
- Logs (logger `apps.geofence.service`): every enter and leave, every push sent, and every reason a push was not sent (`visit_started`, `inner_ring_first`, switches off). Bad salon values are logged as WARNING with the reason, for example "primaryRadiusM=0.8 m is outside 10..50000 m".
