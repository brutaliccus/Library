import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import api, { applyApiBaseUrl } from "../api/client";
import { needsInstanceUrl } from "../api/instanceUrl";

interface AuthUser {
  username: string;
  role: string;
  mustChangePassword: boolean;
}

interface AuthContextType {
  user: AuthUser | null;
  isLoading: boolean;
  /** False until startup auth check (incl. /auth/me) has finished. */
  sessionReady: boolean;
  setupRequired: boolean;
  login: (username: string, password: string) => Promise<void>;
  setup: (username: string, password: string) => Promise<void>;
  logout: () => void;
  clearMustChangePassword: () => void;
  /** Re-fetch whether the instance still needs first-admin setup. */
  refreshSetupRequired: () => Promise<boolean>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [sessionReady, setSessionReady] = useState(false);
  const [setupRequired, setSetupRequired] = useState(false);

  const refreshSetupRequired = useCallback(async () => {
    applyApiBaseUrl();
    try {
      const { data } = await api.get("/auth/setup-required");
      const needed = !!data.setup_required;
      setSetupRequired(needed);
      return needed;
    } catch {
      return setupRequired;
    }
  }, [setupRequired]);

  useEffect(() => {
    const init = async () => {
      setSessionReady(false);
      applyApiBaseUrl();

      // Native APK with no server URL yet — show login so the user can enter it.
      if (needsInstanceUrl()) {
        setUser(null);
        setSetupRequired(false);
        setIsLoading(false);
        setSessionReady(true);
        return;
      }

      const token = localStorage.getItem("access_token");

      // Optimistic paint: if we have a cached user from a prior session, show the
      // shell immediately and validate in the background (no blank "Loading…").
      const cachedUsername = localStorage.getItem("username");
      const cachedRole = localStorage.getItem("user_role");
      if (token && cachedUsername && cachedRole) {
        setUser({
          username: cachedUsername,
          role: cachedRole,
          mustChangePassword: localStorage.getItem("must_change_password") === "true",
        });
        setIsLoading(false);
        setSessionReady(true);
      }

      if (token) {
        // Logged-in path: validate the session. Skip the /auth/setup-required
        // round-trip entirely (a token means setup already happened).
        try {
          const { data } = await api.get("/auth/me");
          localStorage.setItem("user_role", data.role);
          localStorage.setItem("username", data.username);
          localStorage.setItem(
            "must_change_password",
            String(data.must_change_password)
          );
          setUser({
            username: data.username,
            role: data.role,
            mustChangePassword: data.must_change_password,
          });
        } catch {
          localStorage.removeItem("access_token");
          localStorage.removeItem("refresh_token");
          localStorage.removeItem("user_role");
          localStorage.removeItem("username");
          localStorage.removeItem("must_change_password");
          setUser(null);
          // Token was invalid — find out whether to show setup or login.
          try {
            const { data } = await api.get("/auth/setup-required");
            setSetupRequired(!!data.setup_required);
          } catch {
            // server unreachable
          }
        }
      } else {
        // No token: only now do we need setup-required (setup vs login screen).
        try {
          const { data } = await api.get("/auth/setup-required");
          setSetupRequired(!!data.setup_required);
        } catch {
          // server unreachable
        }
      }
      setIsLoading(false);
      setSessionReady(true);
    };
    init();
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const { data } = await api.post("/auth/login", { username, password });
    localStorage.setItem("access_token", data.access_token);
    localStorage.setItem("refresh_token", data.refresh_token);
    localStorage.setItem("user_role", data.role);
    localStorage.setItem("username", data.username);
    localStorage.setItem("must_change_password", String(data.must_change_password));
    setUser({ username: data.username, role: data.role, mustChangePassword: data.must_change_password });
  }, []);

  const setup = useCallback(async (username: string, password: string) => {
    const { data } = await api.post("/auth/setup", { username, password });
    localStorage.setItem("access_token", data.access_token);
    localStorage.setItem("refresh_token", data.refresh_token);
    localStorage.setItem("user_role", data.role);
    localStorage.setItem("username", data.username);
    localStorage.setItem("must_change_password", "false");
    setUser({ username: data.username, role: data.role, mustChangePassword: false });
    setSetupRequired(false);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    localStorage.removeItem("user_role");
    localStorage.removeItem("username");
    localStorage.removeItem("must_change_password");
    setUser(null);
  }, []);

  const clearMustChangePassword = useCallback(() => {
    localStorage.setItem("must_change_password", "false");
    setUser((prev) => prev ? { ...prev, mustChangePassword: false } : null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        isLoading,
        sessionReady,
        setupRequired,
        login,
        setup,
        logout,
        clearMustChangePassword,
        refreshSetupRequired,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
