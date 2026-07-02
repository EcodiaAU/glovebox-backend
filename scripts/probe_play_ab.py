"""A/B the Play SA across packages to localise the 403.

If edits.insert succeeds on au.ecodia.chambers (SA provably ships it) but 403s on
au.ecodia.roam, then au.ecodia.roam lives under a DIFFERENT Play developer account
than the one the SA is a member of - an account-link problem, not a permission grant.
"""

import json
import time

import jwt
import requests

KEY = "D:/PRIVATE/ecodia-creds/play/play-uploader-key.json"
PKGS = ["au.ecodia.chambers", "au.ecodia.roam"]
SCOPE = "https://www.googleapis.com/auth/androidpublisher"

k = json.load(open(KEY))
now = int(time.time())
assertion = jwt.encode(
    {
        "iss": k["client_email"],
        "scope": SCOPE,
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

print(f"SA={k['client_email']}\n")
for pkg in PKGS:
    r = requests.post(f"{base}/applications/{pkg}/edits", headers=h, timeout=25)
    verdict = (
        "OK - SA is a member of this app's account"
        if r.status_code == 200
        else r.json().get("error", {}).get("message", r.text[:120])
    )
    print(f"[edits.insert {pkg:22}] HTTP {r.status_code}  {verdict}")
    if r.status_code == 200:
        eid = r.json().get("id")
        requests.delete(f"{base}/applications/{pkg}/edits/{eid}", headers=h, timeout=20)
