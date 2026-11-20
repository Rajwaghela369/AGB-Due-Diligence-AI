import { createAuthClient, createInternalNeonAuth } from "@neondatabase/auth";
import { BetterAuthReactAdapter } from "@neondatabase/auth/react/adapters";

const authUrl = import.meta.env.VITE_NEON_AUTH_URL?.trim() ?? "";
const authEnabled = import.meta.env.VITE_NEON_AUTH_ENABLED === "true";

/** Neon Auth is deliberately opt-in so the existing auth-disabled local mode is unchanged. */
export const neonAuthConfigured = authEnabled && authUrl.length > 0;

const neonAuthInternal = neonAuthConfigured
  ? createInternalNeonAuth(authUrl, { adapter: BetterAuthReactAdapter() })
  : null;

export const neonAuth = neonAuthConfigured
  ? createAuthClient(authUrl, { adapter: BetterAuthReactAdapter() })
  : null;

/** One token source is shared by REST, downloads and SSE requests. */
export async function getNeonAccessToken(): Promise<string | null> {
  return neonAuthInternal?.getJWTToken() ?? null;
}
