import { useQuery } from "@tanstack/react-query";
import api from "../api/client";
import { libraryQueryKey } from "../utils/libraryQueryKeys";

export interface LibraryMember {
  id: number;
  username: string;
  libraryRole: "owner" | "admin" | "member";
  isOwner: boolean;
}

export type KeySource = "group" | "server" | "none";

export interface LibraryGroupInfo {
  id: number;
  name: string;
  /** Root-relative cover path, e.g. /api/libraries/3/cover */
  coverUrl?: string | null;
  role: "owner" | "admin" | "member";
  isOwner: boolean;
  canManageKeys: boolean;
  hasRdToken: boolean;
  hasTorboxToken: boolean;
  rdKeySource: KeySource;
  torboxKeySource: KeySource;
  usesServerKeys: boolean;
  inviteCode: string | null;
  /** Full share URL from server APP_URL, e.g. https://host/join/CODE */
  inviteLink: string | null;
  /** Library-wide default UI theme (ocean | ember | forest | dusk) */
  defaultTheme?: string;
  members?: LibraryMember[];
}

export interface LibraryGroupResponse {
  library: LibraryGroupInfo | null;
  /** Server .env / Admin → Config already has RD and/or Torbox keys. */
  serverDebridConfigured?: boolean;
}

/** The user's library group; `library: null` means onboarding is required. */
export function useLibraryGroup(enabled = true) {
  return useQuery({
    queryKey: libraryQueryKey("library-group"),
    queryFn: async () => {
      const { data } = await api.get("/libraries/me");
      return data as LibraryGroupResponse;
    },
    enabled,
    staleTime: 60_000,
  });
}
