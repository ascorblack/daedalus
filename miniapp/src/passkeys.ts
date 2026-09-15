// Passkeys in the browser. WebAuthn speaks ArrayBuffers where JSON has base64url, in both
// directions, so every call is wrapped here: the screens only ever see plain objects.

import { api } from "./api";

export type PasskeyView = { id: number; name: string; created_at: string; last_used_at: string | null; transports: string[] };

/** Whether this browser has WebAuthn at all (an http page that is not localhost has not). */
export function supported(): boolean {
  return typeof window.PublicKeyCredential === "function" && !!navigator.credentials;
}

function decode(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function encode(buffer: ArrayBuffer): string {
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

type Descriptor = { id: string; type: string; transports?: string[] };

function descriptors(list: Descriptor[] | undefined): PublicKeyCredentialDescriptor[] | undefined {
  return list?.map((d) => ({ id: decode(d.id), type: "public-key", transports: d.transports as AuthenticatorTransport[] | undefined }));
}

/** Enrol this device: the server's options, the authenticator's answer, the server's verdict. */
export async function enrol(name: string): Promise<PasskeyView[]> {
  const options = await api.post<any>("/api/auth/passkeys/register/begin");
  const { ceremony, ...publicKey } = options;
  const credential = (await navigator.credentials.create({
    publicKey: {
      ...publicKey,
      challenge: decode(options.challenge),
      user: { ...options.user, id: decode(options.user.id) },
      excludeCredentials: descriptors(options.excludeCredentials),
      // The authenticator says whether it made the key discoverable; the server refuses one that did not.
      extensions: { ...(options.extensions ?? {}), credProps: true },
    },
  })) as PublicKeyCredential | null;
  if (!credential) throw new Error("the browser returned no passkey");
  const response = credential.response as AuthenticatorAttestationResponse;
  const done = await api.post<{ passkeys: PasskeyView[] }>("/api/auth/passkeys/register/finish", {
    name,
    ceremony,
    credential: {
      clientExtensionResults: credential.getClientExtensionResults?.() ?? {},
      id: credential.id,
      rawId: encode(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
      response: {
        clientDataJSON: encode(response.clientDataJSON),
        attestationObject: encode(response.attestationObject),
        transports: response.getTransports?.() ?? [],
      },
    },
  });
  return done.passkeys;
}

/** Sign in with a passkey already on this device; the server answers with the session cookie. */
export async function signIn(): Promise<void> {
  const options = await api.post<any>("/api/auth/passkeys/login/begin");
  const { ceremony, ...publicKey } = options;
  const credential = (await navigator.credentials.get({
    publicKey: { ...publicKey, challenge: decode(options.challenge), allowCredentials: descriptors(options.allowCredentials) },
  })) as PublicKeyCredential | null;
  if (!credential) throw new Error("no passkey was offered");
  const response = credential.response as AuthenticatorAssertionResponse;
  await api.post("/api/auth/passkeys/login/finish", {
    ceremony,
    credential: {
      id: credential.id,
      rawId: encode(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
      response: {
        clientDataJSON: encode(response.clientDataJSON),
        authenticatorData: encode(response.authenticatorData),
        signature: encode(response.signature),
        userHandle: response.userHandle ? encode(response.userHandle) : undefined,
      },
    },
  });
}

/** Every browser session ends, this one is re-issued: for a lost device or a link that went astray. */
export function signOutEverywhere(): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>("/api/auth/sessions/revoke");
}

export function list(): Promise<PasskeyView[]> {
  return api.get<PasskeyView[]>("/api/auth/passkeys");
}

export function forget(id: number): Promise<{ passkeys: PasskeyView[] }> {
  return api.delete<{ passkeys: PasskeyView[] }>(`/api/auth/passkeys/${id}`);
}
