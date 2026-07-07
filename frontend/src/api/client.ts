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
