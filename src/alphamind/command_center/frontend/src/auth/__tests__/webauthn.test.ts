import { describe, expect, it, vi } from 'vitest'

import {
  base64UrlDecode,
  base64UrlEncode,
  createRegistrationCredential,
  getAuthenticationCredential,
  type LoginBeginEnvelope,
  type RegisterBeginEnvelope,
} from '../webauthn'

// Build a fake credential and stub navigator.credentials at module scope so
// the nested-callback budget inside each `it` stays under the lint cap of 2.

function buildRegisterFake() {
  return {
    rawId: new Uint8Array([1, 2, 3]).buffer,
    response: {
      clientDataJSON: new Uint8Array([4, 5, 6]).buffer,
      attestationObject: new Uint8Array([7, 8, 9]).buffer,
      getTransports: () => ['internal'],
    },
  }
}

function buildAuthFake() {
  const authData = new Uint8Array(37)
  // Sign count 42 at offset 33 as big-endian uint32.
  authData[33] = 0
  authData[34] = 0
  authData[35] = 0
  authData[36] = 42
  return {
    rawId: new Uint8Array([1]).buffer,
    response: {
      clientDataJSON: new Uint8Array([2]).buffer,
      authenticatorData: authData.buffer,
      signature: new Uint8Array([3]).buffer,
      userHandle: new Uint8Array([4]).buffer,
    },
  }
}

function buildRegisterEnvelope(): RegisterBeginEnvelope {
  return {
    challenge_token: 'tok',
    challenge: base64UrlEncode(new Uint8Array([10])),
    user_id: base64UrlEncode(new Uint8Array([20])),
    user_name: 'op',
    relying_party_id: 'localhost',
    relying_party_name: 'Test RP',
    existing_credentials: [],
  }
}

function buildAuthEnvelope(): LoginBeginEnvelope {
  return {
    challenge_token: 'authtok',
    challenge: base64UrlEncode(new Uint8Array([0])),
    relying_party_id: 'localhost',
    allow_credentials: [base64UrlEncode(new Uint8Array([1]))],
  }
}

// Build vi.fn() at module scope so the inline arrow `() => Promise.resolve(...)`
// doesn't push the per-it nesting count over the lint cap (max 2).
function makeResolvingFn(value: unknown) {
  return vi.fn(() => Promise.resolve(value as Credential))
}

describe('base64url round-trip', () => {
  it('encodes and decodes without loss', () => {
    const original = new Uint8Array([0, 1, 2, 3, 254, 255])
    const encoded = base64UrlEncode(original)
    const decoded = base64UrlDecode(encoded)
    expect([...decoded]).toEqual([...original])
  })

  it('strips padding from encoded output', () => {
    expect(base64UrlEncode(new Uint8Array([1]))).not.toContain('=')
  })
})

describe('createRegistrationCredential', () => {
  it('drives navigator.credentials.create() and shapes the response', async () => {
    const create = makeResolvingFn(buildRegisterFake())
    vi.stubGlobal('navigator', { credentials: { create, get: vi.fn() } })
    const payload = await createRegistrationCredential(buildRegisterEnvelope())

    expect(create).toHaveBeenCalledTimes(1)
    expect(payload.challenge_token).toBe('tok')
    expect(payload.transports).toEqual(['internal'])
    expect([...base64UrlDecode(payload.credential_id)]).toEqual([1, 2, 3])
    expect([...base64UrlDecode(payload.client_data_json)]).toEqual([4, 5, 6])
    expect([...base64UrlDecode(payload.attestation_object)]).toEqual([7, 8, 9])
  })
})

describe('getAuthenticationCredential', () => {
  it('drives navigator.credentials.get() and parses sign count', async () => {
    const get = makeResolvingFn(buildAuthFake())
    vi.stubGlobal('navigator', { credentials: { create: vi.fn(), get } })
    const payload = await getAuthenticationCredential(buildAuthEnvelope())

    expect(get).toHaveBeenCalledTimes(1)
    expect(payload.challenge_token).toBe('authtok')
    expect(payload.new_sign_count).toBe(42)
    expect(payload.user_handle).not.toBeNull()
  })
})
