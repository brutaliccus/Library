import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import api from "../api/client";
import { useAuth } from "../hooks/useAuth";
import { useLibraryGroup } from "../hooks/useLibraryGroup";
import {
  applyThemeToDocument,
  DEFAULT_THEME,
  normalizeThemeId,
  readCachedTheme,
  type ThemeId,
} from "./themes";

interface UserSettingsTheme {
  theme: string | null;
  library_default_theme?: string;
  effective_theme?: string;
}

/**
 * Applies the effective UI theme (user preference, else library default).
 *
 * Important: while settings are still loading, do NOT apply DEFAULT_THEME.
 * That speculative ocean→real-theme bounce was re-toggling Android activity
 * aliases and killing the WebView process ("crashes twice then stabilizes").
 */
export function useThemeSync() {
  const { user, sessionReady } = useAuth();
  const needsSettings =
    !!user && sessionReady && !user.mustChangePassword && !user.mustSetEmail;
  const libraryQuery = useLibraryGroup(needsSettings);
  const settingsQuery = useQuery({
    queryKey: ["user-settings"],
    queryFn: async () => {
      const { data } = await api.get("/auth/settings");
      return data as UserSettingsTheme;
    },
    enabled: needsSettings,
    staleTime: 60_000,
  });

  const settingsReady = !needsSettings || settingsQuery.isFetched;

  const effective: ThemeId = (() => {
    if (settingsQuery.data?.effective_theme) {
      return normalizeThemeId(settingsQuery.data.effective_theme);
    }
    const personal = settingsQuery.data?.theme;
    if (personal) return normalizeThemeId(personal);
    const libDefault = libraryQuery.data?.library?.defaultTheme;
    if (libDefault) return normalizeThemeId(libDefault);
    return readCachedTheme() ?? DEFAULT_THEME;
  })();

  useEffect(() => {
    if (!settingsReady) return;
    applyThemeToDocument(effective);
  }, [effective, settingsReady]);

  return {
    effectiveTheme: effective,
    libraryDefaultTheme: normalizeThemeId(
      settingsQuery.data?.library_default_theme ||
        libraryQuery.data?.library?.defaultTheme ||
        DEFAULT_THEME
    ),
    personalTheme: settingsQuery.data?.theme
      ? normalizeThemeId(settingsQuery.data.theme)
      : null,
  };
}
