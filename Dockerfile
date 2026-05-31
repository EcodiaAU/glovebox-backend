FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# curl + ca-certificates: needed by scripts/fetch-apple-root-certs.sh during
# the build step below so app/services/apple_receipt.py can verify StoreKit
# JWS signatures in production.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY scripts /app/scripts
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/scripts/fetch-apple-root-certs.sh

# Bake Apple root certs into the image so the v2 entitlement/redeem path
# (app/services/apple_receipt.py) can verify StoreKit JWS signatures.
# Cloud Run env var APPLE_ROOT_CERT_BUNDLE_PATH=/app/app/data/apple-roots
# points the verifier at this directory.
RUN /app/scripts/fetch-apple-root-certs.sh /app/app/data/apple-roots

EXPOSE 8080

CMD ["/app/entrypoint.sh"]