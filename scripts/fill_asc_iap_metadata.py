#!/usr/bin/env python3
"""
Fill ASC IAP metadata via App Store Connect API for the 3 v2 Glovebox passes.

For each IAP, probes current state and POSTs whatever's missing:
  - Localization (en-AU display name + description)
  - Price schedule (manual AUD price -> mapped price point)
  - Territory availability (default to all territories)

Idempotent: re-running after a partial fill skips already-set pieces.
"""

import json
import time
from pathlib import Path

import jwt
import requests

KEY_ID = "R8P6K38X47"
ISSUER_ID = "4b45186b-49e4-4a25-8a63-afd28cf12d3f"
P8_PATH = Path("D:/PRIVATE/ecodia-creds/apple/AuthKey_R8P6K38X47.p8")
BASE = "https://api.appstoreconnect.apple.com"

IAPS = [
    {
        "id": "6775312593",
        "product_id": "glovebox_pass_month",
        "name": "Glovebox Month Pass",
        "description": "30 days unlimited trips and AI guide",
        "price_aud": "9.99",
    },
    {
        "id": "6775313497",
        "product_id": "glovebox_pass_season",
        "name": "Glovebox Season Pass",
        "description": "90 days unlimited trips and AI guide",
        "price_aud": "19.99",
    },
    {
        "id": "6775314670",
        "product_id": "glovebox_lifetime",
        "name": "Glovebox Lifetime",
        "description": "Unlimited trips and AI guide, forever",
        "price_aud": "34.99",
    },
]


def mint_token():
    key = P8_PATH.read_text()
    now = int(time.time())
    payload = {
        "iss": ISSUER_ID,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
    }
    headers = {"kid": KEY_ID, "typ": "JWT"}
    return jwt.encode(payload, key, algorithm="ES256", headers=headers)


def api(method, path, token, **kw):
    url = f"{BASE}{path}" if path.startswith("/") else path
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    h.update(kw.pop("headers", {}))
    r = requests.request(method, url, headers=h, timeout=30, **kw)
    return r


def get_iap_full(iap_id, token):
    """Pull the IAP + its localizations + price schedule + availability via include."""
    r = api(
        "GET",
        f"/v2/inAppPurchases/{iap_id}?include=inAppPurchaseLocalizations,iapPriceSchedule,iapAvailability",
        token,
    )
    return r


def list_localizations(iap_id, token):
    r = api("GET", f"/v2/inAppPurchases/{iap_id}/inAppPurchaseLocalizations", token)
    return r


def create_localization(iap_id, name, desc, locale, token):
    body = {
        "data": {
            "type": "inAppPurchaseLocalizations",
            "attributes": {"name": name, "description": desc, "locale": locale},
            "relationships": {
                "inAppPurchaseV2": {"data": {"type": "inAppPurchases", "id": iap_id}}
            },
        }
    }
    r = api("POST", "/v1/inAppPurchaseLocalizations", token, data=json.dumps(body))
    return r


def get_price_points(iap_id, token):
    """Find the AUD price points for this IAP so we can set a price schedule."""
    r = api(
        "GET",
        f"/v2/inAppPurchases/{iap_id}/pricePoints?filter[territory]=AUS&limit=200",
        token,
    )
    return r


def get_existing_price_schedule(iap_id, token):
    r = api("GET", f"/v2/inAppPurchases/{iap_id}/iapPriceSchedule", token)
    return r


def create_price_schedule(iap_id, price_point_id, token):
    """Set baseTerritory=AUS and manual price = AUD price point."""
    body = {
        "data": {
            "type": "inAppPurchasePriceSchedules",
            "relationships": {
                "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                "baseTerritory": {"data": {"type": "territories", "id": "AUS"}},
                "manualPrices": {
                    "data": [{"type": "inAppPurchasePrices", "id": "${price1}"}]
                },
            },
        },
        "included": [
            {
                "type": "inAppPurchasePrices",
                "id": "${price1}",
                "attributes": {"startDate": None},
                "relationships": {
                    "inAppPurchaseV2": {
                        "data": {"type": "inAppPurchases", "id": iap_id}
                    },
                    "inAppPurchasePricePoint": {
                        "data": {
                            "type": "inAppPurchasePricePoints",
                            "id": price_point_id,
                        }
                    },
                },
            }
        ],
    }
    r = api("POST", "/v1/inAppPurchasePriceSchedules", token, data=json.dumps(body))
    return r


def get_availability(iap_id, token):
    r = api("GET", f"/v2/inAppPurchases/{iap_id}/iapAvailability", token)
    return r


def list_all_territories(token):
    r = api("GET", "/v1/territories?limit=200", token)
    return r


def create_availability(iap_id, territory_ids, token):
    body = {
        "data": {
            "type": "inAppPurchaseAvailabilities",
            "attributes": {"availableInNewTerritories": True},
            "relationships": {
                "inAppPurchase": {"data": {"type": "inAppPurchases", "id": iap_id}},
                "availableTerritories": {
                    "data": [
                        {"type": "territories", "id": tid} for tid in territory_ids
                    ]
                },
            },
        }
    }
    r = api("POST", "/v1/inAppPurchaseAvailabilities", token, data=json.dumps(body))
    return r


def find_aud_price_point(price_points_json, target_aud):
    """Walk the price-point list to find the one whose customerPrice matches target."""
    for pp in price_points_json.get("data", []):
        attrs = pp.get("attributes", {})
        customer_price = attrs.get("customerPrice")
        if customer_price and str(customer_price) == target_aud:
            return pp["id"]
    return None


def main():
    token = mint_token()
    print("[+] JWT minted (1200s lifetime)")
    print()

    # Pull territory list once for availability
    terr_resp = list_all_territories(token)
    territory_ids = []
    if terr_resp.status_code == 200:
        territory_ids = [t["id"] for t in terr_resp.json().get("data", [])]
        print(f"[+] {len(territory_ids)} territories discovered")
    else:
        print(
            f"[!] territory list failed: {terr_resp.status_code} {terr_resp.text[:300]}"
        )

    summary = []

    for iap in IAPS:
        print(f"\n=== {iap['product_id']} (id={iap['id']}) ===")
        result = {"product_id": iap["product_id"], "id": iap["id"], "actions": []}

        # ---- 1) Localization ----
        loc_resp = list_localizations(iap["id"], token)
        if loc_resp.status_code != 200:
            print(
                f"  [!] loc list failed: {loc_resp.status_code} {loc_resp.text[:200]}"
            )
        else:
            existing_locales = {
                l["attributes"]["locale"] for l in loc_resp.json().get("data", [])
            }
            print(f"  [+] existing locales: {sorted(existing_locales) or '(none)'}")
            for locale in ("en-AU", "en-US"):
                if locale in existing_locales:
                    result["actions"].append(f"loc:{locale}:skip-exists")
                    continue
                cr = create_localization(
                    iap["id"], iap["name"], iap["description"], locale, token
                )
                if cr.status_code in (200, 201):
                    print(f"  [OK] created localization {locale}: {iap['name']}")
                    result["actions"].append(f"loc:{locale}:created")
                else:
                    print(
                        f"  [!] create loc {locale} failed: {cr.status_code} {cr.text[:300]}"
                    )
                    result["actions"].append(f"loc:{locale}:fail-{cr.status_code}")

        # ---- 2) Price schedule ----
        sched_resp = get_existing_price_schedule(iap["id"], token)
        has_schedule = sched_resp.status_code == 200 and sched_resp.json().get("data")
        if has_schedule:
            print("  [+] price schedule already set, skipping")
            result["actions"].append("price:skip-exists")
        else:
            pp_resp = get_price_points(iap["id"], token)
            if pp_resp.status_code != 200:
                print(
                    f"  [!] price points fetch failed: {pp_resp.status_code} {pp_resp.text[:200]}"
                )
                result["actions"].append(f"price:fail-pp-fetch-{pp_resp.status_code}")
            else:
                pp_id = find_aud_price_point(pp_resp.json(), iap["price_aud"])
                if not pp_id:
                    available = [
                        p["attributes"].get("customerPrice")
                        for p in pp_resp.json().get("data", [])[:20]
                    ]
                    print(
                        f"  [!] no AUD price point matches {iap['price_aud']}. First 20: {available}"
                    )
                    result["actions"].append(f"price:fail-no-pp-{iap['price_aud']}")
                else:
                    print(f"  [+] AUD price point for ${iap['price_aud']} = {pp_id}")
                    ps = create_price_schedule(iap["id"], pp_id, token)
                    if ps.status_code in (200, 201):
                        print(f"  [OK] price schedule set to ${iap['price_aud']} AUD")
                        result["actions"].append(f"price:created-${iap['price_aud']}")
                    else:
                        print(
                            f"  [!] price schedule POST failed: {ps.status_code} {ps.text[:400]}"
                        )
                        result["actions"].append(f"price:fail-{ps.status_code}")

        # ---- 3) Availability ----
        av_resp = get_availability(iap["id"], token)
        has_avail = av_resp.status_code == 200 and av_resp.json().get("data")
        if has_avail:
            print("  [+] availability already configured, skipping")
            result["actions"].append("avail:skip-exists")
        elif territory_ids:
            ar = create_availability(iap["id"], territory_ids, token)
            if ar.status_code in (200, 201):
                print(
                    f"  [OK] availability set: {len(territory_ids)} territories + auto-new-territories"
                )
                result["actions"].append(f"avail:created-{len(territory_ids)}")
            else:
                print(f"  [!] avail POST failed: {ar.status_code} {ar.text[:300]}")
                result["actions"].append(f"avail:fail-{ar.status_code}")

        summary.append(result)

    print("\n\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
