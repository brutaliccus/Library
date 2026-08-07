import { QueryClient } from "@tanstack/react-query";

/** Shared app QueryClient used by boot persist and per-library cache hydration. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});
