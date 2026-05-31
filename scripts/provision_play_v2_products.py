#!/usr/bin/env python3
"""Create the three v2 in-app products on Google Play for au.ecodia.roam.

Run once by the conductor with a service-account JSON that has
`androidpublisher` scope and is added to the Play Console -> Users for
au.ecodia.roam:

    GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH=path/to/sa.json \\
        python scripts/provision_play_v2_products.py

The script uses androidpublisher v3 `inappproducts.insert`. Idempotent:
catches existing-resource errors and reports them.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass


@dataclass
class V2PlaySku:
    sku: str
    title: str
    description: str
    price_micros: int  # AUD micros (1000000 = $1)
    purchase_type: str  # "managedUser" (one-time)


SKUS = [
    V2PlaySku(
        sku="glovebox_pass_month",
        title="Glovebox Month Pass",
        description="30 days of unlimited trips, AI guide, offline maps.",
        price_micros=9_990_000,
        purchase_type="managedUser",
    ),
    V2PlaySku(
        sku="glovebox_pass_season",
        title="Glovebox Season Pass",
        description="90 days of unlimited trips, AI guide, offline maps.",
        price_micros=19_990_000,
        purchase_type="managedUser",
    ),
    V2PlaySku(
        sku="glovebox_lifetime",
        title="Glovebox Lifetime",
        description="One payment, unlimited forever.",
        price_micros=34_990_000,
        purchase_type="managedUser",
    ),
]

PACKAGE_NAME = "au.ecodia.roam"


def main() -> int:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        sys.exit(
            "install deps first: "
            "pip install google-api-python-client>=2.150.0 google-auth>=2.34.0"
        )

    path = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH", "")
    if not path or not os.path.exists(path):
        sys.exit(
            f"GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH={path!r} is required and "
            "must point to an existing JSON file"
        )

    with open(path, "r", encoding="utf-8") as f:
        sa_info = json.load(f)

    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/androidpublisher"],
    )
    service = build("androidpublisher", "v3", credentials=creds, cache_discovery=False)

    print(f"\n# Glovebox v2 Play provisioning for {PACKAGE_NAME}\n")

    for sku in SKUS:
        body = {
            "packageName": PACKAGE_NAME,
            "sku": sku.sku,
            "status": "active",
            "purchaseType": sku.purchase_type,
            "defaultPrice": {
                "priceMicros": str(sku.price_micros),
                "currency": "AUD",
            },
            "listings": {
                "en-AU": {
                    "title": sku.title,
                    "description": sku.description,
                }
            },
            "defaultLanguage": "en-AU",
        }
        try:
            response = (
                service.inappproducts()
                .insert(packageName=PACKAGE_NAME, body=body)
                .execute()
            )
            print(f"[created] {sku.sku} -> {response.get('sku')}")
        except HttpError as exc:
            status = getattr(exc, "status_code", 0) or (
                exc.resp.status if hasattr(exc, "resp") else 0
            )
            if status == 409:
                # Already exists - try to update prices in case they drifted.
                try:
                    response = (
                        service.inappproducts()
                        .update(
                            packageName=PACKAGE_NAME,
                            sku=sku.sku,
                            body=body,
                        )
                        .execute()
                    )
                    print(f"[exists, updated] {sku.sku}")
                except HttpError as exc2:
                    print(f"[exists, update failed] {sku.sku}: {exc2}")
            else:
                print(f"[error] {sku.sku}: {exc}")

    print("\n# Done. Verify in Play Console -> Monetize -> In-app products.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
