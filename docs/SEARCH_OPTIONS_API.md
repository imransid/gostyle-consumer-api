# Smart Search: the option lists and the word chips

For the mobile team. One new route, `GET /api/v1/search/options`, that gives the Smart
Search Matrix its lists, and two new params on the results routes
(`docs/SEARCH_RESULTS_API.md`): `quick` and `service_tags`.
Behind the switch `SEARCH_OPTIONS_V1`: while it is off, the route answers 404 and the
two params are ignored.

No token needed.

---

## 1. `GET /api/v1/search/options`

The same shape as the map drawer (`GET /me/map/filters`): a list of `sections`. Each
section's `key` is the param the results routes take, and each option's `id` is a value
that param accepts.

```json
{
  "sections": [
    {"key": "tab", "title": "Search in", "multiple": false, "default": ["near_me"],
     "options": [{"id": "near_me", "label": "Near Me"}, {"id": "packages", "label": "Packages"}]},
    {"key": "quick", "title": "Quick picks", "multiple": false, "default": [],
     "options": [{"id": "cuts_fade", "label": "Cuts & Fade", "icon": null},
                 {"id": "beard_shave", "label": "Beard & Shave", "icon": null}]},
    {"key": "amenities", "title": "Amenities", "multiple": true, "default": [],
     "options": [{"id": "parking", "label": "Parking"}, {"id": "packages", "label": "Packages Use"}]},
    {"key": "service_tags", "title": "Services", "multiple": true, "default": [],
     "groups": [
       {"key": "cuts_styling", "title": "Cuts & Styling",
        "options": [{"id": "fade", "label": "Fade", "icon": null}]},
       {"key": "beard_shave", "title": "Beard & Shave",
        "options": [{"id": "hot_towel", "label": "Hot Towel", "icon": null},
                    {"id": "shave", "label": "Shave", "icon": null}]}
     ]}
  ]
}
```

- `multiple: false`: send one id. `multiple: true`: send the ids comma separated.
- `default` is what the screen opens with. `tab` opens on `near_me`, which needs the
  phone's position: without location, send no `tab`.
- `service_tags` has `groups` instead of `options`: one row per group, with its title.
- `icon` is an icon name when one is set, else `null`: use your own icon for that id.
- `title` says what the section is; print your own if the design has none.

**Only chips with data are sent.** A chip that no salon could match today is left out,
and so is a section or a group left with no chip. So the list changes as salons add
services: read it each time the screen opens (the server caches it for 60 seconds).

**Not in the list at all yet** (no data anywhere, they wait on the client): Mall Shops,
Home Service, Offers (20 Pts), 20pt Deals, Membership Card, Metro Near, Verified, and
the Regions / Major Hubs / Neighbourhood rows. Do not show those chips.

---

## 2. Sending the picks: `quick` and `service_tags`

On both `GET /api/v1/search/salons` and `GET /api/v1/search/services`:

| Param | Value | Salons tab | Services tab |
| ----- | ----- | ---------- | ------------ |
| `quick` | one id from the `quick` section | salons with a service that matches the chip | the services that match it |
| `service_tags` | ids from the `service_tags` groups, comma separated | salons with a service that matches **any** of them | the services that match any of them |

With both, both must hold. The other sections map as they are: `tab` to `tab`,
`amenities` to `amenities`.

```
GET /api/v1/search/services?quick=cuts_fade&service_tags=fade&latitude=25.2048&longitude=55.2708
```

**What "matches" means:** each chip stands for a few words (for example Fade = "fade",
Beard Trim = "beard trim", "beard"). A service matches when one of the words is in its
name, in its category's name, or in one of its tags. Case is ignored.

An id that is no longer in the list (removed since the screen loaded it) is ignored, not
an error: the results are as if it was not sent.

The "Apply" button's number: call `GET /search/salons` with the picks and `pageSize=1`,
and read `count`.

---

## 3. For the server team

- The chips live in our own table `search_tag` (migrations `salons` 0003 and 0004, which
  also add the Figma's 12 chips). They run when the container starts.
- They are edited in the admin, **Salons > Search tags**: label, words (a JSON list such
  as `["fade", "skin fade"]`), order, active. A change shows in the app at once.
- Never change a chip's `slug` once the app has it: it is the id the app sends.
