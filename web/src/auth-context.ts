import { createContext, useContext } from "react";

export type AuthSession = {
  user: { name?: string | null; email?: string | null };
  session?: { token?: string };
};

export interface AuthState {
  configured: boolean;
  pending: boolean;
  session: AuthSession | null;
  error: string | null;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

export const disabledState: AuthState = {
  configured: false,
  pending: false,
  session: null,
  error: null,
  signIn: async () => undefined,
  signOut: async () => undefined,
};

export const AuthContext = createContext<AuthState>(disabledState);

export function useAuth(): AuthState {
  return useContext(AuthContext);
}
