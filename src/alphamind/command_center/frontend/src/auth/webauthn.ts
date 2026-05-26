// Browser-side WebAuthn primitives.
//
// Bridges between py_webauthn's base64url-encoded JSON envelopes (story 03's
// /auth/{register,login}/{begin,complete}) and the browser's native
// `PublicKeyCredentialCreationOptions` / `PublicKeyCredentialRequestOptions`
// shapes. The post-Wave-3-followup response bodies expect:
//
//   RegistrationResponse: credential_id, client_data_json (b64url str),
//     attestation_object (b64url str), transports (optional list).
//   AuthenticationResponse: credential_id, client_data_json,
//     authenticator_data, signature, user_handle (optional), new_sign_count.
//
// The setup-token flow + the registration/login forms call into these
// helpers; tests inject a fake `navigator.credentials` to drive the ceremony
// without a real authenticator.

// ---------------------------------------------------------------------------
// base64url <-> ArrayBuffer/Uint8Array
// ---------------------------------------------------------------------------

export function base64UrlEncode(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  let binary = ''
  for (const byte of view) {
    binary += String.fromCodePoint(byte)
  }
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')
}

export function base64UrlDecode(text: string): Uint8Array {
  const padded = text.replaceAll('-', '+').replaceAll('_', '/')
  const padLength = (4 - (padded.length % 4)) % 4
  const binary = atob(padded + '='.repeat(padLength))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.codePointAt(i) ?? 0
  }
  return bytes
}

// Browser WebAuthn options require strict `BufferSource = ArrayBuffer |
// ArrayBufferView<ArrayBuffer>`. `Uint8Array` constructed from atob() has a
// generic `ArrayBufferLike` backing under TS5.7+'s lib.dom, which is no
// longer assignable to the narrowed BufferSource. Construct a fresh
// `ArrayBuffer` directly so the type is the strict one the lib needs.
function toBufferSource(text: string): ArrayBuffer {
  const decoded = base64UrlDecode(text)
  const buffer = new ArrayBuffer(decoded.byteLength)
  new Uint8Array(buffer).set(decoded)
  return buffer
}

// ---------------------------------------------------------------------------
// Registration ceremony
// ---------------------------------------------------------------------------

export type RegisterBeginEnvelope = {
  challenge_token: string
  challenge: string
  user_id: string
  user_name: string
  relying_party_id: string
  relying_party_name: string
  existing_credentials: string[]
}

export type RegisterCompletePayload = {
  challenge_token: string
  credential_id: string
  client_data_json: string
  attestation_object: string
  transports: string[]
}

// Drive navigator.credentials.create() against the begin envelope and shape
// the result into the body the /register/complete endpoint expects.
//
// Allowed-credential excludes prior credentials so the user can't accidentally
// re-enroll a passkey that's already registered.
export async function createRegistrationCredential(
  envelope: RegisterBeginEnvelope,
): Promise<RegisterCompletePayload> {
  const publicKey: PublicKeyCredentialCreationOptions = {
    challenge: toBufferSource(envelope.challenge),
    rp: {
      id: envelope.relying_party_id,
      name: envelope.relying_party_name,
    },
    user: {
      id: toBufferSource(envelope.user_id),
      name: envelope.user_name,
      displayName: envelope.user_name,
    },
    pubKeyCredParams: [
      { type: 'public-key', alg: -7 }, // ES256
      { type: 'public-key', alg: -257 }, // RS256
    ],
    authenticatorSelection: {
      userVerification: 'preferred',
      residentKey: 'preferred',
    },
    excludeCredentials: envelope.existing_credentials.map((id) => ({
      id: toBufferSource(id),
      type: 'public-key',
    })),
    timeout: 60_000,
    attestation: 'none',
  }
  const credential = await navigator.credentials.create({ publicKey })
  if (credential === null) {
    throw new Error('navigator.credentials.create() returned null')
  }
  const cred = credential as PublicKeyCredential
  const response = cred.response as AuthenticatorAttestationResponse
  const transports = typeof response.getTransports === 'function' ? response.getTransports() : []
  return {
    challenge_token: envelope.challenge_token,
    credential_id: base64UrlEncode(cred.rawId),
    client_data_json: base64UrlEncode(response.clientDataJSON),
    attestation_object: base64UrlEncode(response.attestationObject),
    transports,
  }
}

// ---------------------------------------------------------------------------
// Authentication ceremony
// ---------------------------------------------------------------------------

export type LoginBeginEnvelope = {
  challenge_token: string
  challenge: string
  relying_party_id: string
  allow_credentials: string[]
}

export type LoginCompletePayload = {
  challenge_token: string
  credential_id: string
  client_data_json: string
  authenticator_data: string
  signature: string
  user_handle: string | null
  new_sign_count: number
}

// Drive navigator.credentials.get() against the begin envelope and shape
// the result into the body the /login/complete endpoint expects.
//
// Per the Wave 3 followup, new_sign_count flows from the authenticator
// response's `signCount`; the server validates strict-increase against the
// stored counter. Authenticators that don't support a counter report 0
// forever — story 03's verifier accepts 0/0 as legitimate (constraint
// ge=0, not ge=1).
export async function getAuthenticationCredential(
  envelope: LoginBeginEnvelope,
): Promise<LoginCompletePayload> {
  const publicKey: PublicKeyCredentialRequestOptions = {
    challenge: toBufferSource(envelope.challenge),
    rpId: envelope.relying_party_id,
    allowCredentials: envelope.allow_credentials.map((id) => ({
      id: toBufferSource(id),
      type: 'public-key',
    })),
    userVerification: 'preferred',
    timeout: 60_000,
  }
  const credential = await navigator.credentials.get({ publicKey })
  if (credential === null) {
    throw new Error('navigator.credentials.get() returned null')
  }
  const cred = credential as PublicKeyCredential
  const response = cred.response as AuthenticatorAssertionResponse
  return {
    challenge_token: envelope.challenge_token,
    credential_id: base64UrlEncode(cred.rawId),
    client_data_json: base64UrlEncode(response.clientDataJSON),
    authenticator_data: base64UrlEncode(response.authenticatorData),
    signature: base64UrlEncode(response.signature),
    user_handle: response.userHandle === null ? null : base64UrlEncode(response.userHandle),
    new_sign_count: extractSignCount(response.authenticatorData),
  }
}

// authenticatorData layout: 32 bytes RP id hash, 1 byte flags, 4 bytes
// sign count (big-endian uint32), then optional attested cred data + ext.
// We pull the sign count at offset 33 — small enough to inline rather than
// pull in a parser dependency.
function extractSignCount(authenticatorData: ArrayBuffer): number {
  const view = new DataView(authenticatorData)
  return view.getUint32(33, false)
}
