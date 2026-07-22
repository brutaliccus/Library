import { useCallback, useEffect, useState } from "react";
import { Check, Download, Loader2, Trash2 } from "lucide-react";
import { useToast } from "../contexts/ToastContext";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import {
  absDownloadState,
  downloadAbsOffline,
  downloadEbookOffline,
  downloadRdOffline,
  ebookDownloadState,
  rdDownloadState,
  removeAbsOffline,
  removeEbookOffline,
  removeRdOffline,
  type OfflineDownloadState,
} from "../utils/downloadOffline";
import type { CacheableTrack } from "../utils/audioCache";

type Target =
  | { kind: "abs"; itemId: string }
  | {
      kind: "rd";
      libraryItemId?: number;
      streamHistoryId?: number;
      title: string;
      author?: string;
      coverUrl?: string;
      tracks: CacheableTrack[];
      totalDuration?: number;
    }
  | {
      kind: "ebook";
      chapterId: number;
      title: string;
      author?: string;
      coverUrl?: string;
      isPdf?: boolean;
    };

interface Props {
  target: Target;
  className?: string;
  /** Compact icon+label for cards; default is a normal button. */
  size?: "sm" | "md";
}

export default function SaveOfflineButton({ target, className = "", size = "md" }: Props) {
  const { toast } = useToast();
  const online = useOnlineStatus();
  const [state, setState] = useState<OfflineDownloadState>("idle");
  const [progress, setProgress] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      if (target.kind === "abs") {
        setState(await absDownloadState(target.itemId));
      } else if (target.kind === "rd") {
        setState(await rdDownloadState(target));
      } else {
        setState(await ebookDownloadState(target.chapterId, target.isPdf ?? true));
      }
    } catch {
      setState("idle");
    }
  }, [target]);

  useEffect(() => {
    void refresh();
    const onAudio = () => void refresh();
    const onEbook = () => void refresh();
    window.addEventListener("audio-cache-updated", onAudio);
    window.addEventListener("ebook-cache-updated", onEbook);
    return () => {
      window.removeEventListener("audio-cache-updated", onAudio);
      window.removeEventListener("ebook-cache-updated", onEbook);
    };
  }, [refresh]);

  const startDownload = async (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    if (!online) {
      toast("Connect to download this book", "info");
      return;
    }
    setState("downloading");
    setProgress("");
    try {
      if (target.kind === "abs") {
        await downloadAbsOffline(target.itemId, (done, total) => {
          setProgress(`${done}/${total}`);
        });
      } else if (target.kind === "rd") {
        await downloadRdOffline({
          ...target,
          onProgress: (done, total) => setProgress(`${done}/${total}`),
        });
      } else {
        await downloadEbookOffline(target);
      }
      setState("downloaded");
      toast("Saved offline", "success");
    } catch (err) {
      setState("error");
      toast(err instanceof Error ? err.message : "Download failed", "error");
      void refresh();
    } finally {
      setProgress("");
    }
  };

  const remove = async (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    try {
      if (target.kind === "abs") await removeAbsOffline(target.itemId);
      else if (target.kind === "rd") await removeRdOffline(target);
      else await removeEbookOffline(target.chapterId);
      setState("idle");
      toast("Removed from this device", "info");
    } catch {
      toast("Could not remove download", "error");
    }
  };

  const pad =
    size === "sm"
      ? "px-2 py-1 text-[11px] gap-1 rounded-md"
      : "px-3 py-2 text-sm gap-1.5 rounded-lg";

  if (state === "downloaded") {
    return (
      <button
        type="button"
        onClick={remove}
        title="Remove from this device"
        className={`inline-flex items-center font-medium bg-gray-800 text-emerald-400 border border-gray-700 hover:border-red-700 hover:text-red-300 transition-colors ${pad} ${className}`}
      >
        <Check size={size === "sm" ? 12 : 14} />
        Downloaded
        <Trash2 size={size === "sm" ? 11 : 13} className="opacity-60" />
      </button>
    );
  }

  if (state === "downloading") {
    return (
      <button
        type="button"
        disabled
        className={`inline-flex items-center font-medium bg-gray-800 text-gray-300 border border-gray-700 ${pad} ${className}`}
      >
        <Loader2 size={size === "sm" ? 12 : 14} className="animate-spin" />
        {progress ? `Saving ${progress}` : "Saving…"}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={(e) => void startDownload(e)}
      disabled={!online}
      title={online ? "Save offline" : "Connect to save offline"}
      className={`inline-flex items-center font-medium bg-gray-800 text-gray-200 border border-gray-700 hover:bg-gray-700 transition-colors disabled:opacity-40 ${pad} ${className}`}
    >
      <Download size={size === "sm" ? 12 : 14} />
      Save offline
    </button>
  );
}
