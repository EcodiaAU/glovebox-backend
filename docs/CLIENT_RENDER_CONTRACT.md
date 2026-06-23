# Glovebox backend -> client render contract

Single source of truth for what the `roam-backend` supplies and what a client
must FETCH and RENDER to be at full parity. Web, iOS, and Android all target
this. Status columns reflect the 2026-06-23 cross-client audit.

Legend: ✅ rendered · 🟡 fetched but not rendered (or partial) · ❌ not consumed · n/a not applicable

Backend base URL: `https://roam-backend-2z5escjq6a-ts.a.run.app`

## Endpoints (supply)

| Capability | Endpoint | Web | iOS | Android |
|---|---|---|---|---|
| Primary route (turn-by-turn) | POST /nav/route | ✅ | ✅ | ✅ |
| Elevation profile + grades | POST /nav/elevation | 🟡 typed, not fetched | ✅ | 🟡 fetched as overlay only |
| Route intelligence score (0-10 + factor breakdown) | POST /nav/route-score | ❌ | ✅ card | 🟡 fetched, value not shown |
| Places along corridor (rich, multi-category) | POST /places/corridor | ❌ | ✅ | ✅ |
| Places sampled along route | POST /places/suggest | ❌ | ✅ | ✅ |
| Place search (query/bbox) | POST /places/search | ✅ basic | ✅ | ✅ |
| Stop suggestions | POST /places/stop-suggestions | ❌ | ❌ | ✅ |
| Offline bundle build + download | POST /bundle/build, GET /bundle/{id}/download | 🟡 downloaded, overlays orphaned | ✅ (+ phased nav tier) | n/a (uses MapLibre OfflineManager region) |

## Overlays (18; shipped in the bundle and as /nav/<x>/along-route|poll)

Each overlay must render on the map AND, where it has per-feature detail, in a tappable detail/list.

| Overlay | Web | iOS | Android |
|---|---|---|---|
| traffic | 🟡 headline only | ✅ | ✅ symbol |
| hazards | 🟡 title only | ✅ | ✅ symbol |
| weather (forecast along route) | ❌ | ✅ | ✅ symbol |
| flood (catchments + gauges) | ❌ | ✅ | ✅ symbol |
| fuel stations + **station-level prices** | ❌ | ✅ map; price detail partial | 🟡 stations shown, **prices not rendered** |
| EV chargers + **connector types** | ❌ | ✅ map marker; connector detail partial | 🟡 **not rendered** |
| coverage (mobile reception gaps) | ❌ | ✅ | ✅ symbol |
| wildlife risk zones | ❌ | ✅ | ✅ symbol |
| rest_areas | ❌ | ✅ | ✅ symbol |
| emergency services | ❌ | ✅ | ✅ symbol |
| heritage | ❌ | ✅ | ✅ symbol |
| air_quality (AQI) | ❌ | ✅ | ✅ symbol |
| bushfire incidents + hotspots | ❌ | ✅ | ✅ symbol |
| speed_cameras + black spots | ❌ | ✅ | ✅ symbol |
| toilets + dump points | ❌ | ✅ | ✅ symbol |
| school_zones | ❌ | ✅ | ✅ symbol |
| roadkill risk | ❌ | ✅ | ✅ symbol |
| route_score (as map context) | ❌ | ✅ card | 🟡 value not shown |

## Per-POI rich `extra` fields (~30)

Canonical full render = a place detail card showing: description (+ Wikipedia
attribution when present), photo (thumbnail_url), opening_hours, phone, website,
address, and the category-specific facts below.

| Field group | Fields | Web | iOS | Android |
|---|---|---|---|---|
| Blurb | description / short_description, wiki_attribution, wiki_source | ❌ not in schema | ✅ | 🟡 **description + wiki not rendered** |
| Photo | thumbnail_url | ❌ | ✅ hero + lightbox | ✅ |
| Contact | phone, website, address, opening_hours | ❌ | ✅ | ✅ |
| Food | cuisine, diets | ❌ | ✅ | ✅ |
| Camp/stay | camp_type, num_sites, max_stay_days, surface, pets_allowed, fires_allowed, shade, max_vehicle_length_m, price_per_night_aud, price_notes, check_in/out, quiet_hours, bookable, overnight_allowed/max_hours/notes | ❌ | ✅ | ✅ |
| Facilities | has_water/water_type, has_toilets/toilet_type, has_showers/shower_type, has_dump_point/dump_type, powered_sites, bbq/kitchen/laundry/wifi/swimming/playground | ❌ | ✅ | ✅ |
| Reception | has_phone_reception, reception_carriers, socket_types, internet_access | ❌ | ✅ (socket_types/internet 🟡) | ✅ (partial) |
| Fuel | fuel_types + per-type prices | ❌ | 🟡 partial | 🟡 **prices not rendered** |
| Misc | stars, elevation_m, wheelchair/accessible, quality_score | ❌ | ✅ (quality_score 🟡) | ✅ |

## Parity gaps to close (prioritised by value)

P1 (highest user value):
- **Fuel station prices** rendered on every client (blocked on the live NSW
  fuel data-query fix, worker cowork.glovebox-nsw-fuel-dataquery-fix; QLD/SA/VIC
  already return prices). Once backend serves prices, render per-station price
  in the fuel POI detail on web/iOS/Android.
- **Android**: render POI description + Wikipedia attribution, route-score value,
  fuel prices, EV connector types (all fetched, none shown).
- **Web**: render the 18 overlays it already downloads (currently orphaned in
  IndexedDB) and add the rich POI fields to its PlaceResult schema + detail UI.

P2:
- iOS: wire /places/stop-suggestions; render socket_types / internet_access /
  quality_score (fetched, unused).
- Web: wire /places/corridor + /places/suggest (corridor POIs) and route-score.
- Overlay DETAIL on Android: symbols show name only; add per-feature detail
  (severity, score, station price) on tap.

## Verify gate (per client, per capability)

A capability is "at parity" only when a discriminating probe shows it rendered
against real backend data on the deployed surface: web = CDP screenshot of the
rendered overlay/field at glovebox.ecodia.au; iOS = sim screenshot of the view;
Android = emulator screenshot. Code-present is not parity.
