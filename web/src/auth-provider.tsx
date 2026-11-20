import { type ReactNode } from "react";

import { neonAuth } from "./auth";
import { AuthContext, disabledState, type AuthState, type AuthSession } from "./auth-context";

export function NeonAuthProvider({ children }: { children: ReactNode }) {
  if (!neonAuth) {
    return <AuthContext.Provider value={disabledState}>{children}</AuthContext.Provider>;
  }
  return <ConfiguredAuthProvider>{children}</ConfiguredAuthProvider>;
}

function ConfiguredAuthProvider({ children }: { children: ReactNode }) {
  const sessionState = neonAuth!.useSession();
  const session = (sessionState.data ?? null) as AuthSession | null;

  const value: AuthState = {
    configured: true,
    pending: sessionState.isPending,
    session,
    error: null,
    signIn: async (email, password) => {
      const result = await neonAuth!.signIn.email({ email, password });
      if (result.error) throw new Error(result.error.message || "Sign-in failed");
    },
    signOut: async () => {
      const result = await neonAuth!.signOut();
      if (result.error) throw new Error(result.error.message || "Sign-out failed");
    },
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
