import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import api from "../api/client";

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
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [sessionReady, setSessionReady] = useState(false);
  const [setupRequired, setSetupRequired] = useState(false);

  useEffect(() => {
    const init = async () => {
      setSessionReady(false);
      try {
        const { data } = await api.get("/auth/setup-required");
        if (data.setup_required) {
          setSetupRequired(true);
          setIsLoading(false);
          setSessionReady(true);
          return;
        }
      } catch {
        // server unreachable
      }

      const token = localStorage.getItem("access_token");
      if (token) {
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
      value={{ user, isLoading, sessionReady, setupRequired, login, setup, logout, clearMustChangePassword }}
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
