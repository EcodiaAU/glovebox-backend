"""Probe au.ecodia.glovebox Play state now that an AAB is on the internal track.
Tells us: is the bundle there, what tracks exist, and which billing API is usable
(legacy inappproducts vs new monetization.onetimeproducts).
"""

import json
import time

import jwt
import requests

PKG = "au.ecodia.glovebox"
k = json.load(open("D:/PRIVATE/ecodia-creds/play/play-uploader-key.json"))
now = int(time.time())
a = jwt.encode(
    {
        "iss": k["client_email"],
        "scope": "https://www.googleapis.com/auth/androidpublisher",
        "aud": k["token_uri"],
        "iat": now,
        "exp": now + 3600,
    },
    k["private_key"],
    algorithm="RS256",
)
tok = requests.post(
    k["token_uri"],
    data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": a},
    timeout=20,
).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}
base = "https://androidpublisher.googleapis.com/androidpublisher/v3"
app = f"{base}/applications/{PKG}"


def show(label, r):
    print(f"[{label}] HTTP {r.status_code}")
    body = r.text[:500].replace("\n", " ")
    print("   " + body)


# 1. bundles on the app (via an edit)
eid = requests.post(f"{app}/edits", headers=h, timeout=25).json().get("id")
if eid:
    show(
        "bundles.list",
        requests.get(f"{app}/edits/{eid}/bundles", headers=h, timeout=25),
    )
    show(
        "tracks.list", requests.get(f"{app}/edits/{eid}/tracks", headers=h, timeout=25)
    )
    requests.delete(f"{app}/edits/{eid}", headers=h, timeout=20)

# 2. legacy inappproducts (does it work now an AAB exists?)
show(
    "inappproducts.list (legacy)",
    requests.get(f"{app}/inappproducts", headers=h, timeout=25),
)

# 3. new monetization one-time products API
show(
    "onetimeproducts.list (new)",
    requests.get(f"{app}/onetimeproducts", headers=h, timeout=25),
)

# 4. new monetization subscriptions API (for completeness)
show(
    "subscriptions.list (new)",
    requests.get(f"{app}/subscriptions", headers=h, timeout=25),
)
