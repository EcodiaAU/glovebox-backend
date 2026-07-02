// verify-entitlement
//
// Self-contained, Apple-API-key-free entitlement verification for the Glovebox
// native iOS app.
//
// The client (StoreKitClient -> EntitlementService.redeem) sends the StoreKit 2
// SIGNED transaction - the `jwsRepresentation` string off a
// `VerificationResult`. StoreKit 2 has already cryptographically verified that
// transaction on-device; this function re-verifies the JWS signature against
// Apple's public certificate chain (the x5c array in the JWS protected header,
// which must chain to Apple's pinned Root CA - G3), so the server trusts the
// payload without ever calling the App Store Server API or holding an Apple key.
//
// On a verified transaction it maps productId -> tier, derives the row, and
// upserts into public.entitlements keyed by original_transaction_id using the
// SERVICE ROLE (RLS-bypassing). It returns the resolved Entitlement JSON the
// native client decodes ({ tier, expiresAt, source }). Any unverifiable input
// is rejected with 4xx so the client fails safe to Free.
//
// No function secrets beyond the platform-injected SUPABASE_URL +
// SUPABASE_SERVICE_ROLE_KEY (both present automatically in every Supabase Edge
// Function runtime). Apple's root cert is embedded below.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2.39.7";

// ── Apple Root CA - G3 (the root of the StoreKit JWS cert chain) ────────────
// Pinned by SHA-256 fingerprint of the DER. The leaf cert that signs the JWS
// chains leaf -> intermediate (Apple WWDR / G6) -> this root. We verify each
// link's signature and require the chain to terminate at exactly this root.
// Fingerprint source: Apple Root Certificate Authority - G3 (AppleRootCA-G3).
const APPLE_ROOT_CA_G3_SHA256 =
  "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179";

interface DecodedTransaction {
  productId?: string;
  originalTransactionId?: string;
  transactionId?: string;
  expiresDate?: number; // ms epoch, present for subscriptions
  purchaseDate?: number;
  environment?: string; // "Production" | "Sandbox"
  type?: string; // "Auto-Renewable Subscription" | "Non-Consumable" | "Non-Renewing Subscription"
  appAccountToken?: string;
}

// ── Product -> tier mapping (mirror of GBProduct + LegacyV1Product) ─────────
// roam_unlimited is the v1 non-consumable that grandfathers to lifetime.
function tierForProduct(productId: string): "month" | "season" | "lifetime" | null {
  switch (productId) {
    case "glovebox_pass_month":
      return "month";
    case "glovebox_pass_season":
      return "season";
    case "glovebox_lifetime":
      return "lifetime";
    case "roam_unlimited":
      return "lifetime"; // v1 grandfather
    default:
      return null;
  }
}

// Non-renewing passes carry no Apple expiresDate, so the server derives expiry
// from purchaseDate + the pass duration (mirror GBProduct.entitlementDuration).
const PASS_DURATION_MS: Record<string, number> = {
  glovebox_pass_month: 30 * 24 * 60 * 60 * 1000,
  glovebox_pass_season: 90 * 24 * 60 * 60 * 1000,
};

// ── base64url helpers ───────────────────────────────────────────────────────
function b64urlToBytes(b64url: string): Uint8Array {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(
    Math.ceil(b64url.length / 4) * 4,
    "=",
  );
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return bytesToHex(new Uint8Array(digest));
}

// ── Minimal DER / X.509 reader ──────────────────────────────────────────────
// Enough to walk a certificate: extract the TBSCertificate bytes, the
// signature, the SubjectPublicKeyInfo, and to verify one cert's signature with
// the issuer's public key. We only need ECDSA P-256 (Apple's StoreKit chain is
// all prime256v1 / ES256).
interface DerNode {
  tag: number;
  // byte range of the *content* (excluding tag + length header)
  contentStart: number;
  contentEnd: number;
  // byte range of the whole element (tag .. end of content)
  start: number;
  end: number;
}

function readDer(buf: Uint8Array, offset: number): DerNode {
  const tag = buf[offset];
  let i = offset + 1;
  let len = buf[i++];
  if (len & 0x80) {
    const n = len & 0x7f;
    len = 0;
    for (let k = 0; k < n; k++) len = (len << 8) | buf[i++];
  }
  return {
    tag,
    contentStart: i,
    contentEnd: i + len,
    start: offset,
    end: i + len,
  };
}

// Parse the bits of an X.509 cert we need.
interface ParsedCert {
  der: Uint8Array;
  tbsBytes: Uint8Array; // raw TBSCertificate (what the signature is over)
  signature: Uint8Array; // DER ECDSA signature value
  spki: Uint8Array; // SubjectPublicKeyInfo DER (for importing the public key)
}

function parseCert(der: Uint8Array): ParsedCert {
  // Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signatureValue }
  const cert = readDer(der, 0);
  const tbs = readDer(der, cert.contentStart);
  const tbsBytes = der.slice(tbs.start, tbs.end);

  // signatureAlgorithm SEQUENCE follows tbs
  const sigAlg = readDer(der, tbs.end);
  // signatureValue BIT STRING follows signatureAlgorithm
  const sigBitString = readDer(der, sigAlg.end);
  // BIT STRING content: first byte is unused-bits count (0), rest is the DER sig
  const signature = der.slice(sigBitString.contentStart + 1, sigBitString.contentEnd);

  // Walk TBSCertificate to the SubjectPublicKeyInfo.
  // TBSCertificate ::= SEQUENCE {
  //   [0] version, serialNumber INTEGER, signature SEQUENCE, issuer SEQUENCE,
  //   validity SEQUENCE, subject SEQUENCE, subjectPublicKeyInfo SEQUENCE, ... }
  let p = tbs.contentStart;
  let node = readDer(der, p);
  if (node.tag === 0xa0) { // [0] EXPLICIT version
    p = node.end;
    node = readDer(der, p);
  }
  // serialNumber INTEGER
  p = node.end;
  node = readDer(der, p); // signature SEQUENCE
  p = node.end;
  node = readDer(der, p); // issuer SEQUENCE
  p = node.end;
  node = readDer(der, p); // validity SEQUENCE
  p = node.end;
  node = readDer(der, p); // subject SEQUENCE
  p = node.end;
  const spkiNode = readDer(der, p); // subjectPublicKeyInfo SEQUENCE
  const spki = der.slice(spkiNode.start, spkiNode.end);

  return { der, tbsBytes, signature, spki };
}

// ECDSA P-256 signatures in X.509 / JWS:
//  - X.509 cert signatures are DER-encoded (SEQUENCE { r INTEGER, s INTEGER }).
//  - WebCrypto ECDSA verify wants the raw 64-byte (r||s) concatenation.
//  - JWS ES256 signatures are already raw 64-byte (r||s).
function derEcdsaToRaw(der: Uint8Array): Uint8Array {
  const seq = readDer(der, 0);
  let p = seq.contentStart;
  const rNode = readDer(der, p);
  let r = der.slice(rNode.contentStart, rNode.contentEnd);
  p = rNode.end;
  const sNode = readDer(der, p);
  let s = der.slice(sNode.contentStart, sNode.contentEnd);

  const norm = (x: Uint8Array): Uint8Array => {
    // strip leading zero padding
    let xi = 0;
    while (xi < x.length - 1 && x[xi] === 0) xi++;
    x = x.slice(xi);
    // left-pad to 32
    if (x.length > 32) x = x.slice(x.length - 32);
    const out = new Uint8Array(32);
    out.set(x, 32 - x.length);
    return out;
  };

  const out = new Uint8Array(64);
  out.set(norm(r), 0);
  out.set(norm(s), 32);
  return out;
}

async function importEcPublicKey(spki: Uint8Array): Promise<CryptoKey> {
  return await crypto.subtle.importKey(
    "spki",
    spki,
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["verify"],
  );
}

// Verify `signed.tbsBytes` was signed by `issuerSpki` (ECDSA P-256 / SHA-256).
async function verifyCertSignature(
  signed: ParsedCert,
  issuerSpki: Uint8Array,
): Promise<boolean> {
  const key = await importEcPublicKey(issuerSpki);
  const rawSig = derEcdsaToRaw(signed.signature);
  return await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" },
    key,
    rawSig,
    signed.tbsBytes,
  );
}

// ── JWS verification against Apple's pinned root ────────────────────────────
// A StoreKit 2 JWS is `protectedHeaderB64url.payloadB64url.signatureB64url`.
// The protected header carries `x5c`: [leafDerB64, intermediateDerB64,
// rootDerB64]. We:
//   1. parse the chain,
//   2. require the root's SHA-256 to equal the pinned Apple Root CA - G3,
//   3. verify intermediate is signed by root, leaf is signed by intermediate,
//   4. verify the JWS signature with the leaf's public key,
//   5. return the decoded payload.
async function verifyAppleJWS(jws: string): Promise<DecodedTransaction> {
  const parts = jws.split(".");
  if (parts.length !== 3) throw new Error("malformed JWS");
  const [headerB64, payloadB64, sigB64] = parts;

  const header = JSON.parse(new TextDecoder().decode(b64urlToBytes(headerB64)));
  if (header.alg !== "ES256") throw new Error(`unexpected alg ${header.alg}`);
  const x5c: string[] = header.x5c;
  if (!Array.isArray(x5c) || x5c.length < 2) throw new Error("missing x5c chain");

  const chain = x5c.map((b64) => parseCert(b64ToBytes(b64)));
  const leaf = chain[0];
  const root = chain[chain.length - 1];

  // 2. Pin the root.
  const rootFp = await sha256Hex(root.der);
  if (rootFp !== APPLE_ROOT_CA_G3_SHA256) {
    throw new Error("untrusted root certificate");
  }

  // 3. Verify each link: cert[i] signed by cert[i+1].
  for (let i = 0; i < chain.length - 1; i++) {
    const ok = await verifyCertSignature(chain[i], chain[i + 1].spki);
    if (!ok) throw new Error(`broken chain at link ${i}`);
  }
  // Root is self-signed; pinning by fingerprint already establishes trust.

  // 4. Verify the JWS body with the leaf public key.
  const leafKey = await importEcPublicKey(leaf.spki);
  const signingInput = new TextEncoder().encode(`${headerB64}.${payloadB64}`);
  const sig = b64urlToBytes(sigB64); // ES256 raw r||s
  const sigOk = await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" },
    leafKey,
    sig,
    signingInput,
  );
  if (!sigOk) throw new Error("JWS signature verification failed");

  // 5. Decode the payload.
  return JSON.parse(
    new TextDecoder().decode(b64urlToBytes(payloadB64)),
  ) as DecodedTransaction;
}

// ── HTTP handler ────────────────────────────────────────────────────────────
const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (req.method !== "POST") {
    return json({ error: "method not allowed" }, 405);
  }

  // Resolve the caller from the bearer JWT so we write the row under the right
  // user_id. The function runs with the service role for the DB write, but the
  // user identity comes from the Authorization header the client sends.
  const authHeader = req.headers.get("Authorization") ?? "";
  const token = authHeader.replace(/^Bearer\s+/i, "");
  if (!token) return json({ error: "not authenticated" }, 401);

  const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;

  // User-scoped client to resolve auth.uid() from the bearer.
  const userClient = createClient(supabaseUrl, anonKey, {
    global: { headers: { Authorization: `Bearer ${token}` } },
  });
  const { data: userData, error: userErr } = await userClient.auth.getUser();
  if (userErr || !userData.user) {
    return json({ error: "invalid session" }, 401);
  }
  const userId = userData.user.id;

  // Parse the body. `jws` is the StoreKit 2 signed transaction. `source` is
  // informational provenance (purchase | restore | grandfather).
  let body: { jws?: string; source?: string };
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid body" }, 400);
  }
  const jws = body.jws?.trim();
  if (!jws) return json({ error: "missing jws" }, 400);

  // Verify the transaction against Apple's chain. Anything unverifiable is a
  // hard reject so the client fails safe to Free.
  let tx: DecodedTransaction;
  try {
    tx = await verifyAppleJWS(jws);
  } catch (e) {
    return json({ error: `verification failed: ${e.message}` }, 400);
  }

  const productId = tx.productId;
  const originalTransactionId = tx.originalTransactionId ?? tx.transactionId;
  if (!productId || !originalTransactionId) {
    return json({ error: "transaction missing product/id" }, 400);
  }
  const tier = tierForProduct(productId);
  if (!tier) return json({ error: `unknown product ${productId}` }, 400);

  // Resolve expiry: subscriptions carry expiresDate; non-renewing passes derive
  // from purchaseDate + duration; lifetime / non-consumable has no expiry.
  let expiresAtISO: string | null = null;
  if (tier !== "lifetime") {
    if (typeof tx.expiresDate === "number") {
      expiresAtISO = new Date(tx.expiresDate).toISOString();
    } else if (
      typeof tx.purchaseDate === "number" && PASS_DURATION_MS[productId]
    ) {
      expiresAtISO = new Date(tx.purchaseDate + PASS_DURATION_MS[productId])
        .toISOString();
    }
  }

  const provenance = (() => {
    switch (body.source) {
      case "restore":
        return "restore";
      case "grandfather":
        return "grandfather";
      default:
        return "purchase";
    }
  })();

  // Service-role client for the RLS-bypassing upsert.
  const admin = createClient(supabaseUrl, serviceKey);

  const row = {
    user_id: userId,
    tier,
    expires_at: expiresAtISO,
    source_platform: "ios",
    product_id: productId,
    transaction_id: tx.transactionId ?? originalTransactionId,
    original_transaction_id: originalTransactionId,
    environment: tx.environment ?? null,
    source: provenance,
    raw_receipt: tx as unknown as Record<string, unknown>,
    updated_at: new Date().toISOString(),
  };

  const { error: upsertErr } = await admin
    .from("entitlements")
    .upsert(row, { onConflict: "original_transaction_id" });
  if (upsertErr) {
    return json({ error: `persist failed: ${upsertErr.message}` }, 500);
  }

  // Return the resolved entitlement in the shape EntitlementService decodes:
  // { entitlement: { tier, expiresAt, source } }.
  return json({
    entitlement: {
      tier,
      expiresAt: expiresAtISO,
      source: provenance,
    },
  }, 200);
});

function json(obj: unknown, status: number): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}
