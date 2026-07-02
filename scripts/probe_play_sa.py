"""Diagnose the Play service-account 403 on au.ecodia.roam.

Tests, in order:
  1. Auth (JWT -> access token) succeeds at all.
  2. edits.insert on au.ecodia.roam  -> can the SA open an edit on THIS app?
     (403 here = app-ownership / account-link problem, not a product-scope problem)
  3. inappproducts.list on au.ecodia.roam.
Prints the exact API error body for each.
"""

import json
import time

import jwt  # PyJWT
import requests

KEY = "D:/PRIVATE/ecodia-creds/play/play-uploader-key.json"
PKG = "au.ecodia.roam"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def get_token():
    k = json.load(open(KEY))
    now = int(time.time())
    claim = {
        "iss": k["client_email"],
        "scope": SCOPE,
        "aud": k["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }
    assertion = jwt.encode(claim, k["private_key"], algorithm="RS256")
    r = requests.post(
        k["token_uri"],
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"], k


def main():
    tok, k = get_token()
    print(f"AUTH ok. SA={k['client_email']} project={k['project_id']}")
    h = {"Authorization": f"Bearer {tok}"}
    base = "https://androidpublisher.googleapis.com/androidpublisher/v3"

    # 2. edits.insert
    r = requests.post(f"{base}/applications/{PKG}/edits", headers=h, timeout=25)
    print(f"\n[edits.insert {PKG}] HTTP {r.status_code}")
    print("  " + r.text[:500].replace("\n", "\n  "))
    edit_id = r.json().get("id") if r.status_code == 200 else None

    # 3. inappproducts.list
    r = requests.get(f"{base}/applications/{PKG}/inappproducts", headers=h, timeout=25)
    print(f"\n[inappproducts.list {PKG}] HTTP {r.status_code}")
    print("  " + r.text[:500].replace("\n", "\n  "))

    # clean up the edit if we opened one
    if edit_id:
        requests.delete(
            f"{base}/applications/{PKG}/edits/{edit_id}", headers=h, timeout=20
        )
        print(f"\n(cleaned up edit {edit_id})")


if __name__ == "__main__":
    main()
