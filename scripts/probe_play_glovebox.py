"""Verify the SA now has access on the freshly-created au.ecodia.glovebox Play app."""

import json
import time

import jwt
import requests

k = json.load(open("D:/PRIVATE/ecodia-creds/play/play-uploader-key.json"))
now = int(time.time())
assertion = jwt.encode(
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
    data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    },
    timeout=20,
).json()["access_token"]
h = {"Authorization": f"Bearer {tok}"}
base = "https://androidpublisher.googleapis.com/androidpublisher/v3"

for pkg in ["au.ecodia.glovebox", "au.ecodia.roam"]:
    r = requests.post(f"{base}/applications/{pkg}/edits", headers=h, timeout=25)
    if r.status_code == 200:
        msg = "OK - SA HAS ACCESS"
        eid = r.json()["id"]
        ip = requests.get(
            f"{base}/applications/{pkg}/inappproducts", headers=h, timeout=20
        )
        msg += f" | inappproducts.list HTTP {ip.status_code}"
        if ip.status_code == 200:
            msg += f" count={len(ip.json().get('inappproduct', []))}"
        else:
            msg += " (" + ip.json().get("error", {}).get("message", ip.text[:80]) + ")"
        requests.delete(f"{base}/applications/{pkg}/edits/{eid}", headers=h, timeout=20)
    else:
        msg = str(r.json().get("error", {}).get("message", r.text[:120]))
    print(f"[edits.insert {pkg:20}] HTTP {r.status_code}  {msg}")
