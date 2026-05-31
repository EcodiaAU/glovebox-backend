#!/usr/bin/env python3
"""Create the three v2 Stripe Products + Prices for glovebox-backend.

Run once by the conductor with the glovebox Stripe secret key in env:

    STRIPE_SECRET_KEY=sk_live_... python scripts/provision_stripe_v2_products.py

Outputs the three Price IDs that go into Cloud Run env vars
STRIPE_PRICE_MONTH / STRIPE_PRICE_SEASON / STRIPE_PRICE_LIFETIME.

Idempotent: looks up existing products by name before creating. Safe to
re-run after a partial failure.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

try:
    import stripe as stripe_lib
except ImportError:
    sys.exit("install stripe first: pip install stripe>=8.0.0")


@dataclass
class V2Sku:
    name: str
    description: str
    amount_aud: int  # cents
    metadata_tier: str
    settings_env_var: str


SKUS = [
    V2Sku(
        name="Glovebox - Month Pass",
        description="30 days of unlimited trips, AI guide, offline maps.",
        amount_aud=999,
        metadata_tier="month",
        settings_env_var="STRIPE_PRICE_MONTH",
    ),
    V2Sku(
        name="Glovebox - Season Pass",
        description="90 days of unlimited trips, AI guide, offline maps.",
        amount_aud=1999,
        metadata_tier="season",
        settings_env_var="STRIPE_PRICE_SEASON",
    ),
    V2Sku(
        name="Glovebox - Lifetime",
        description="One payment, unlimited forever.",
        amount_aud=3499,
        metadata_tier="lifetime",
        settings_env_var="STRIPE_PRICE_LIFETIME",
    ),
]


def main() -> int:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        sys.exit("STRIPE_SECRET_KEY env var is required")
    stripe_lib.api_key = key

    print("\n# Glovebox v2 Stripe provisioning")
    print(f"# account: {stripe_lib.api_key[:12]}...\n")

    out_lines: list[str] = []

    for sku in SKUS:
        existing = stripe_lib.Product.search(query=f"name:'{sku.name}'", limit=1)
        if existing.data:
            product = existing.data[0]
            print(f"[exists]  product {sku.name!r} -> {product.id}")
        else:
            product = stripe_lib.Product.create(
                name=sku.name,
                description=sku.description,
                metadata={"v2_tier": sku.metadata_tier},
            )
            print(f"[created] product {sku.name!r} -> {product.id}")

        # Find or create a Price for AUD at the requested amount
        prices = stripe_lib.Price.list(product=product.id, active=True, limit=10)
        price = None
        for p in prices.data:
            if (
                p.currency == "aud"
                and p.unit_amount == sku.amount_aud
                and p.type == "one_time"
            ):
                price = p
                print(f"[exists]  price ${sku.amount_aud / 100:.2f} AUD -> {p.id}")
                break

        if price is None:
            price = stripe_lib.Price.create(
                product=product.id,
                currency="aud",
                unit_amount=sku.amount_aud,
                metadata={"v2_tier": sku.metadata_tier},
            )
            print(f"[created] price ${sku.amount_aud / 100:.2f} AUD -> {price.id}")

        out_lines.append(f"{sku.settings_env_var}={price.id}")

    print("\n# Cloud Run env vars to set:")
    for line in out_lines:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
