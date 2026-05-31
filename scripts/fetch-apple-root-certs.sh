#!/bin/sh
# Download Apple's root certificates needed by the App Store Server JWS
# verifier (app/services/apple_receipt.py) into app/data/apple-roots/.
#
# Run in the Dockerfile build OR by the conductor before the first
# Cloud Run deploy that needs the v2 redeem endpoint live.

set -e

DEST="${1:-app/data/apple-roots}"
mkdir -p "$DEST"

# AppleRootCA-G3 is the SHA-2 root currently signing the WWDR Intermediate
# that signs Apple's app-store-server signing certs. See
# https://www.apple.com/certificateauthority/.
curl -fsSL -o "$DEST/AppleRootCA-G3.cer" \
    https://www.apple.com/certificateauthority/AppleRootCA-G3.cer

# WWDRCAG6 intermediate. Bundle is fine either-or.
curl -fsSL -o "$DEST/AppleWWDRCAG6.cer" \
    https://www.apple.com/certificateauthority/AppleWWDRCAG6.cer

echo "Apple root certs in $DEST:"
ls -la "$DEST"
