import axios from "axios";

const api = axios.create({
  baseURL: "/api",
});

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("access_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Serialize refresh: when multiple requests get 401, only one refresh runs; others wait
let refreshPromise: Promise<string> | null = null;

export const AUTH_TOKEN_REFRESHED_EVENT = "auth-token-refreshed";

function parseJwtExpMs(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

/** Refresh when missing, expired, or expiring within skewMs (used by WebSocket). */
export async function refreshAccessTokenIfNeeded(skewMs = 120_000): Promise<string | null> {
  const token = localStorage.getItem("access_token");
  const refreshToken = localStorage.getItem("refresh_token");
  if (!refreshToken) return token;

  const exp = token ? parseJwtExpMs(token) : null;
  const needsRefresh = !token || exp == null || exp < Date.now() + skewMs;
  if (!needsRefresh) return token;

  try {
    if (!refreshPromise) {
      refreshPromise = axios
        .post("/api/auth/refresh", { refresh_token: refreshToken })
        .then(({ data }) => {
          localStorage.setItem("access_token", data.access_token);
          localStorage.setItem("refresh_token", data.refresh_token);
          localStorage.setItem("user_role", data.role);
          localStorage.setItem("username", data.username);
          localStorage.setItem("must_change_password", String(data.must_change_password));
          window.dispatchEvent(
            new CustomEvent(AUTH_TOKEN_REFRESHED_EVENT, { detail: data.access_token })
          );
          return data.access_token as string;
        })
        .finally(() => {
          refreshPromise = null;
        });
    }
    return await refreshPromise;
  } catch {
    refreshPromise = null;
    return token;
  }
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    if (error.response?.status === 401 && !original._retry) {
      original._retry = true;
      const refreshToken = localStorage.getItem("refresh_token");
      if (refreshToken) {
        try {
          if (!refreshPromise) {
            refreshPromise = axios
              .post("/api/auth/refresh", { refresh_token: refreshToken })
              .then(({ data }) => {
                localStorage.setItem("access_token", data.access_token);
                localStorage.setItem("refresh_token", data.refresh_token);
                localStorage.setItem("user_role", data.role);
                localStorage.setItem("username", data.username);
                localStorage.setItem(
                  "must_change_password",
                  String(data.must_change_password)
                );
                window.dispatchEvent(
                  new CustomEvent(AUTH_TOKEN_REFRESHED_EVENT, { detail: data.access_token })
                );
                return data.access_token;
              })
              .finally(() => {
                refreshPromise = null;
              });
          }
          const newToken = await refreshPromise;
          original.headers.Authorization = `Bearer ${newToken}`;
          return api(original);
        } catch {
          refreshPromise = null;
          localStorage.removeItem("access_token");
          localStorage.removeItem("refresh_token");
          localStorage.removeItem("user_role");
          localStorage.removeItem("username");
          localStorage.removeItem("must_change_password");
          window.location.href = "/login";
        }
      } else {
        localStorage.removeItem("access_token");
        localStorage.removeItem("user_role");
        localStorage.removeItem("username");
        localStorage.removeItem("must_change_password");
        if (!window.location.pathname.startsWith("/login")) {
          window.location.href = "/login";
        }
      }
    }
    return Promise.reject(error);
  }
);

export default api;
