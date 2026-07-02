#!/usr/bin/env python3
"""
Self-contained App Store Connect submit for Glovebox (au.ecodia.roam).
Runs ON SY094 (where PyJWT + the .p8 key live). Attaches the latest VALID
1.1.1 build to the PREPARE_FOR_SUBMISSION App Store Version, then creates +
submits a review submission. Prints clean RESULT: lines that survive terminal
noise. Reuses an in-flight review submission if one already exists.

Usage on SY094:  cd ~/asc-scripts && python3 asc_submit_glovebox.py [--dry] [--build-version N]
  --dry             attach build + report readiness, do NOT submit.
  --build-version N  attach this specific build version (default: latest VALID).
"""

import json
import sys
import time
import urllib.request
import urllib.error

import jwt as pyjwt

DRY = "--dry" in sys.argv
if "--build-version" in sys.argv:
    WANT_BUILD = sys.argv[sys.argv.index("--build-version") + 1]
else:
    WANT_BUILD = None

SPEC = json.load(open("apps/roam.json"))
KEY_ID = SPEC["asc_api_key_id"]
ISS = SPEC["asc_api_issuer_id"]
APP = SPEC["asc_app_id"]
MARKETING = SPEC.get("marketing_version", "1.1.1")
P8 = SPEC["asc_api_p8_path"].replace("~", "/Users/user276189")
KEY = open(P8).read()
BASE = "https://api.appstoreconnect.apple.com"


def tok():
    return pyjwt.encode(
        {"iss": ISS, "exp": int(time.time()) + 900, "aud": "appstoreconnect-v1"},
        KEY,
        algorithm="ES256",
        headers={"kid": KEY_ID, "typ": "JWT"},
    )


def req(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + tok(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(r) as x:
            raw = x.read()
            return x.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode(errors="replace")[:600]}


def errs(d):
    return (
        "; ".join(
            f"{e.get('title', '?')}: {e.get('detail', '')}"[:200]
            for e in d.get("errors", [])
        )
        or json.dumps(d)[:400]
    )


def main():
    # 1. Find the marketing-version ASV (prefer an editable state).
    # NOTE: the appStoreVersions relationship endpoint rejects 'sort'.
    s, d = req("GET", f"/v1/apps/{APP}/appStoreVersions?limit=20")
    if s != 200:
        print("RESULT: FAIL find-asv", errs(d))
        return 1
    asv = None
    asv_state = None
    editable = (
        "PREPARE_FOR_SUBMISSION",
        "DEVELOPER_REJECTED",
        "REJECTED",
        "METADATA_REJECTED",
        "INVALID_BINARY",
    )
    for v in d.get("data", []):
        a = v["attributes"]
        if a["versionString"] == MARKETING and a["appStoreState"] in editable:
            asv = v["id"]
            asv_state = a["appStoreState"]
            break
    if not asv:
        for v in d.get("data", []):
            if v["attributes"]["versionString"] == MARKETING:
                asv = v["id"]
                asv_state = v["attributes"]["appStoreState"]
                break
    if not asv:
        print(f"RESULT: FAIL no-asv for {MARKETING}")
        return 1
    print(f"RESULT: ASV {asv} state={asv_state}")

    # 2. Pick the build (latest VALID non-expired, or a requested version).
    s, d = req("GET", f"/v1/builds?filter[app]={APP}&sort=-version&limit=20")
    if s != 200:
        print("RESULT: FAIL list-builds", errs(d))
        return 1
    build = build_v = None
    for b in d.get("data", []):
        a = b["attributes"]
        if a.get("processingState") != "VALID" or a.get("expired"):
            continue
        if WANT_BUILD and str(a.get("version")) != str(WANT_BUILD):
            continue
        build = b["id"]
        build_v = a["version"]
        break
    if not build:
        print(f"RESULT: FAIL no-valid-build (wanted={WANT_BUILD})")
        return 1
    print(f"RESULT: BUILD {build} v{build_v}")

    # 3. Export-compliance flag must be answered to submit.
    s, d = req("GET", f"/v1/builds/{build}")
    uses_enc = d.get("data", {}).get("attributes", {}).get("usesNonExemptEncryption")
    print(f"RESULT: BUILD_ENCRYPTION usesNonExemptEncryption={uses_enc}")

    # 4. Attach build to ASV.
    s, d = req(
        "PATCH",
        f"/v1/appStoreVersions/{asv}/relationships/build",
        {"data": {"type": "builds", "id": build}},
    )
    if s not in (200, 204):
        print("RESULT: FAIL attach-build", errs(d))
        return 1
    print(f"RESULT: ATTACHED build v{build_v} -> ASV")

    if DRY:
        print("RESULT: DRY done (not submitted)")
        return 0

    # 5. Reuse an in-flight review submission or create one.
    s, d = req(
        "GET",
        f"/v1/reviewSubmissions?filter[app]={APP}&filter[state]=READY_FOR_REVIEW&limit=5",
    )
    rs_id = None
    for r in d.get("data", []):
        rs_id = r["id"]
        break
    if not rs_id:
        s, d = req(
            "POST",
            "/v1/reviewSubmissions",
            {
                "data": {
                    "type": "reviewSubmissions",
                    "attributes": {"platform": "IOS"},
                    "relationships": {"app": {"data": {"type": "apps", "id": APP}}},
                }
            },
        )
        if s not in (200, 201):
            print("RESULT: FAIL create-submission", errs(d))
            return 1
        rs_id = d["data"]["id"]
    print(f"RESULT: REVIEW_SUBMISSION {rs_id}")

    # 6. Add the ASV as a submission item (skip if already present).
    s, d = req("GET", f"/v1/reviewSubmissions/{rs_id}/items?limit=20")
    have = any(
        (it.get("relationships", {}).get("appStoreVersion", {}).get("data") or {}).get(
            "id"
        )
        == asv
        for it in d.get("data", [])
    )
    if not have:
        s, d = req(
            "POST",
            "/v1/reviewSubmissionItems",
            {
                "data": {
                    "type": "reviewSubmissionItems",
                    "relationships": {
                        "reviewSubmission": {
                            "data": {"type": "reviewSubmissions", "id": rs_id}
                        },
                        "appStoreVersion": {
                            "data": {"type": "appStoreVersions", "id": asv}
                        },
                    },
                }
            },
        )
        if s not in (200, 201):
            print("RESULT: FAIL add-item", errs(d))
            return 1
    print("RESULT: ITEM_ADDED")

    # 7. Submit.
    s, d = req(
        "PATCH",
        f"/v1/reviewSubmissions/{rs_id}",
        {
            "data": {
                "type": "reviewSubmissions",
                "id": rs_id,
                "attributes": {"submitted": True},
            }
        },
    )
    if s not in (200, 204):
        print("RESULT: FAIL submit", errs(d))
        return 1
    state = d.get("data", {}).get("attributes", {}).get("state", "?")
    print(f"RESULT: SUBMITTED state={state} submission={rs_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
