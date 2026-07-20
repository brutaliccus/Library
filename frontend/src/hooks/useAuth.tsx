import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import api, { applyApiBaseUrl } from "../api/client";
import {
  getStoredInstanceUrl,
  needsInstanceUrl,
  setInstanceUrl,
  isNativeApp,
} from "../api/instanceUrl";
import {
  clearActiveSession,
  currentOrigin,
  getSessionForOrigin,
  saveSessionForOrigin,
  switchToLibrary,
  upsertRememberedLibrary,
  type LibrarySession,
} from "../api/libraryRegistry";

interface AuthUser {
  username: string;
  email: string | null;
  role: string;
  mustChangePassword: boolean;
  mustSetEmail: boolean;
}

interface SessionTokens {
  access_token: string;
  refresh_token: string;
  role: string;
  username: string;
  email?: string | null;
  must_change_password?: boolean;
  must_set_email?: boolean;
}

interface AuthContextType {
  user: AuthUser | null;
  isLoading: boolean;
  sessionReady: boolean;
  setupRequired: boolean;
  login: (email: string, password: string, origin?: string) => Promise<void>;
  setup: (email: string, password: string, origin?: string) => Promise<void>;
  acceptSession: (data: SessionTokens) => void;
  logout: () => void;
  clearMustChangePassword: () => void;
  applyEmailUpdate: (data: SessionTokens) => void;
  refreshSetupRequired: () => Promise<boolean>;
  rememberCurrentLibrary: () => Promise<void>;
  /** Switch to a saved library; restores session if present. */
  enterLibrary: (origin: string) => Promise<"ok" | "need_login">;
}

const AuthContext = createContext<AuthContextType | null>(null);

function userFromTokens(data: SessionTokens, emailFallback?: string | null): AuthUser {
  return {
    username: data.username,
    email: data.email ?? emailFallback ?? null,
    role: data.role,
    mustChangePassword: !!data.must_change_password,
    mustSetEmail: !!data.must_set_email,
  };
}

function persistSession(data: SessionTokens, origin?: string) {
  const o = (origin || currentOrigin() || getStoredInstanceUrl() || "").replace(/\/+$/, "");
  const session: LibrarySession = {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    role: data.role,
    username: data.username,
    email: data.email ?? null,
    must_change_password: !!data.must_change_password,
    must_set_email: !!data.must_set_email,
  };
  if (o) {
    saveSessionForOrigin(o, session);
  } else {
    localStorage.setItem("access_token", session.access_token);
    localStorage.setItem("refresh_token", session.refresh_token);
    localStorage.setItem("user_role", session.role);
    localStorage.setItem("username", session.username);
    if (session.email) localStorage.setItem("user_email", session.email);
    else localStorage.removeItem("user_email");
    localStorage.setItem("must_change_password", String(session.must_change_password));
    localStorage.setItem("must_set_email", String(session.must_set_email));
  }
}

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

  const rememberCurrentLibrary = useCallback(async () => {
    const origin = currentOrigin();
    const email =
      localStorage.getItem("user_email") ||
      user?.email ||
      user?.username ||
      localStorage.getItem("username") ||
      "";
    if (!origin || !email) return;
    try {
      const { data } = await api.get("/libraries/me");
      const lib = data?.library;
      upsertRememberedLibrary({
        origin,
        name: lib?.name || "Library",
        coverUrl: lib?.coverUrl || null,
        email,
      });
    } catch {
      upsertRememberedLibrary({
        origin,
        name: "Library",
        coverUrl: null,
        email,
      });
    }
  }, [user?.email, user?.username]);

  useEffect(() => {
    const init = async () => {
      setSessionReady(false);
      applyApiBaseUrl();

      if (needsInstanceUrl()) {
        setUser(null);
        setSetupRequired(false);
        setIsLoading(false);
        setSessionReady(true);
        return;
      }

      const token = localStorage.getItem("access_token");
      const cachedUsername = localStorage.getItem("username");
      const cachedRole = localStorage.getItem("user_role");
      const cachedEmail = localStorage.getItem("user_email");
      if (token && cachedUsername && cachedRole) {
        setUser({
          username: cachedUsername,
          email: cachedEmail,
          role: cachedRole,
          mustChangePassword: localStorage.getItem("must_change_password") === "true",
          mustSetEmail: localStorage.getItem("must_set_email") === "true",
        });
        setIsLoading(false);
        setSessionReady(true);
      }

      if (token) {
        try {
          const { data } = await api.get("/auth/me");
          localStorage.setItem("user_role", data.role);
          localStorage.setItem("username", data.username);
          if (data.email) localStorage.setItem("user_email", data.email);
          else localStorage.removeItem("user_email");
          localStorage.setItem(
            "must_change_password",
            String(data.must_change_password)
          );
          localStorage.setItem("must_set_email", String(!!data.must_set_email));
          setUser({
            username: data.username,
            email: data.email ?? null,
            role: data.role,
            mustChangePassword: !!data.must_change_password,
            mustSetEmail: !!data.must_set_email,
          });
          const origin = currentOrigin();
          if (origin) {
            const access = localStorage.getItem("access_token") || "";
            const refresh = localStorage.getItem("refresh_token") || "";
            if (access && refresh) {
              saveSessionForOrigin(origin, {
                access_token: access,
                refresh_token: refresh,
                role: data.role,
                username: data.username,
                email: data.email ?? null,
                must_change_password: !!data.must_change_password,
                must_set_email: !!data.must_set_email,
              });
            }
            if (data.email) {
              try {
                const lib = await api.get("/libraries/me");
                upsertRememberedLibrary({
                  origin,
                  name: lib.data?.library?.name || "Library",
                  coverUrl: lib.data?.library?.coverUrl || null,
                  email: data.email,
                });
              } catch {
                /* ignore */
              }
            }
          }
        } catch {
          clearActiveSession();
          setUser(null);
          try {
            const { data } = await api.get("/auth/setup-required");
            setSetupRequired(!!data.setup_required);
          } catch {
            /* unreachable */
          }
        }
      } else if (!isNativeApp() || getStoredInstanceUrl()) {
        try {
          const { data } = await api.get("/auth/setup-required");
          setSetupRequired(!!data.setup_required);
        } catch {
          /* unreachable */
        }
      }
      setIsLoading(false);
      setSessionReady(true);
    };
    void init();
  }, []);

  const login = useCallback(async (email: string, password: string, origin?: string) => {
    if (origin) {
      setInstanceUrl(origin);
      applyApiBaseUrl();
    }
    const { data } = await api.post("/auth/login", { email, password });
    persistSession(data, origin || currentOrigin());
    setUser(userFromTokens(data, email));
    const o = origin || currentOrigin();
    const identity = data.email || email || data.username;
    if (o && identity) {
      try {
        const lib = await api.get("/libraries/me");
        upsertRememberedLibrary({
          origin: o,
          name: lib.data?.library?.name || "Library",
          coverUrl: lib.data?.library?.coverUrl || null,
          email: identity,
        });
      } catch {
        upsertRememberedLibrary({
          origin: o,
          name: "Library",
          coverUrl: null,
          email: identity,
        });
      }
    }
  }, []);

  const setup = useCallback(async (email: string, password: string, origin?: string) => {
    if (origin) {
      setInstanceUrl(origin);
      applyApiBaseUrl();
    }
    const { data } = await api.post("/auth/setup", { email, password });
    persistSession(
      { ...data, must_change_password: false, must_set_email: false },
      origin || currentOrigin()
    );
    setUser({
      username: data.username,
      email: data.email ?? email,
      role: data.role,
      mustChangePassword: false,
      mustSetEmail: false,
    });
    setSetupRequired(false);
  }, []);

  const acceptSession = useCallback((data: SessionTokens) => {
    const origin = currentOrigin();
    persistSession(data, origin);
    setUser(userFromTokens(data));
    setSetupRequired(false);
    const email = data.email;
    if (origin && email) {
      void api
        .get("/libraries/me")
        .then((lib) => {
          upsertRememberedLibrary({
            origin,
            name: lib.data?.library?.name || "Library",
            coverUrl: lib.data?.library?.coverUrl || null,
            email,
          });
        })
        .catch(() => {
          upsertRememberedLibrary({
            origin,
            name: "Library",
            coverUrl: null,
            email,
          });
        });
    }
  }, []);

  const enterLibrary = useCallback(async (origin: string): Promise<"ok" | "need_login"> => {
    const key = origin.replace(/\/+$/, "");
    const existing = getSessionForOrigin(key);
    switchToLibrary(key);
    applyApiBaseUrl();
    if (!existing) return "need_login";
    try {
      const { data } = await api.get("/auth/me");
      setUser({
        username: data.username,
        email: data.email ?? existing.email,
        role: data.role,
        mustChangePassword: !!data.must_change_password,
        mustSetEmail: !!data.must_set_email,
      });
      localStorage.setItem("must_set_email", String(!!data.must_set_email));
      if (data.email) {
        try {
          const lib = await api.get("/libraries/me");
          upsertRememberedLibrary({
            origin: key,
            name: lib.data?.library?.name || "Library",
            coverUrl: lib.data?.library?.coverUrl || null,
            email: data.email,
          });
        } catch {
          /* ignore */
        }
      }
      return "ok";
    } catch {
      clearActiveSession();
      setUser(null);
      return "need_login";
    }
  }, []);

  const logout = useCallback(() => {
    clearActiveSession();
    setUser(null);
  }, []);

  const clearMustChangePassword = useCallback(() => {
    localStorage.setItem("must_change_password", "false");
    const origin = currentOrigin();
    if (origin) {
      const existing = getSessionForOrigin(origin);
      if (existing) {
        saveSessionForOrigin(origin, { ...existing, must_change_password: false });
      }
    }
    setUser((prev) => (prev ? { ...prev, mustChangePassword: false } : null));
  }, []);

  const applyEmailUpdate = useCallback((data: SessionTokens) => {
    persistSession({ ...data, must_set_email: false }, currentOrigin());
    setUser(userFromTokens({ ...data, must_set_email: false }));
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
        acceptSession,
        logout,
        clearMustChangePassword,
        applyEmailUpdate,
        refreshSetupRequired,
        rememberCurrentLibrary,
        enterLibrary,
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
