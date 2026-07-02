"""Set the au.ecodia.glovebox main store listing (en-GB) via the edits API.
Probes how far the API gets on a brand-new app before an AAB / graphics are required.
"""

import json
import time

import jwt
import requests

PKG = "au.ecodia.glovebox"
LANG = "en-GB"  # Play maps en-AU to en-GB; en-AU-only blocks the editor.
TITLE = "Glovebox"
SHORT = "Offline maps, trip planning and campsite finder for Australian road trips."
FULL = (
    "Glovebox is the offline co-pilot for Australian road trips.\n\n"
    "Download the map before you lose signal and navigate the whole country off-grid - "
    "free camps, caravan parks, dump points, fuel and water, all on a map that works with "
    "no reception.\n\n"
    "- Offline vector maps you download per region\n"
    "- Trip planner with multi-stop routes\n"
    "- Turn-by-turn navigation that keeps working off-grid\n"
    "- Campsite and free-camp finder\n"
    "- Fuel cost estimates for your vehicle\n"
    "- An AI travel guide for the road\n\n"
    "Built for the Big Lap, the weekend escape, and everything in between."
)


def token():
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
    return requests.post(
        k["token_uri"],
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": a,
        },
        timeout=20,
    ).json()["access_token"]


h = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
base = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PKG}"

eid = requests.post(f"{base}/edits", headers=h, timeout=25).json()["id"]
print("edit:", eid)

r = requests.put(
    f"{base}/edits/{eid}/listings/{LANG}",
    headers=h,
    timeout=25,
    data=json.dumps(
        {
            "language": LANG,
            "title": TITLE,
            "shortDescription": SHORT,
            "fullDescription": FULL,
        }
    ),
)
print("listing PUT:", r.status_code, "" if r.status_code == 200 else r.text[:300])

vr = requests.post(f"{base}/edits/{eid}:validate", headers=h, timeout=25)
print("validate:", vr.status_code, "" if vr.status_code == 200 else vr.text[:400])

cr = requests.post(f"{base}/edits/{eid}:commit", headers=h, timeout=25)
print(
    "commit:",
    cr.status_code,
    "OK - listing text live" if cr.status_code == 200 else cr.text[:400],
)
