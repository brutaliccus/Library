import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  FileAudio,
  File as FileIcon,
  Folder,
  FolderOpen,
  Trash2,
} from "lucide-react";
import api from "../../api/client";
import Modal from "../Modal";
import { useToast } from "../../contexts/ToastContext";

type StagingEntry = {
  name: string;
  path: string;
  type: "file" | "dir";
  size: number | null;
  ext: string | null;
  children: StagingEntry[] | null;
};

type StagingTreeResponse = {
  request_id: number;
  title: string;
  status: string;
  staging_path: string;
  root_name: string;
  entries: StagingEntry[];
  entry_count: number;
  truncated: boolean;
};

function formatSize(bytes: number | null | undefined): string {
  if (bytes == null || Number.isNaN(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function isAudioExt(ext: string | null): boolean {
  if (!ext) return false;
  return [".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav", ".wma", ".aac", ".mp4"].includes(
    ext.toLowerCase(),
  );
}

function TreeNode({
  entry,
  depth,
  onDelete,
  deletingPath,
}: {
  entry: StagingEntry;
  depth: number;
  onDelete: (path: string, name: string, isDir: boolean) => void;
  deletingPath: string | null;
}) {
  const [open, setOpen] = useState(depth < 2);
  const isDir = entry.type === "dir";
  const Icon = isDir
    ? open
      ? FolderOpen
      : Folder
    : isAudioExt(entry.ext)
      ? FileAudio
      : FileIcon;

  return (
    <div>
      <div
        className="group flex items-center gap-1.5 py-1 px-1.5 rounded-lg hover:bg-gray-700/50 min-w-0"
        style={{ paddingLeft: 4 + depth * 14 }}
      >
        {isDir ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="p-0.5 text-gray-500 hover:text-gray-300 shrink-0"
            aria-label={open ? "Collapse" : "Expand"}
          >
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
        ) : (
          <span className="w-[18px] shrink-0" />
        )}
        <Icon
          size={14}
          className={
            isDir
              ? "text-amber-400/90 shrink-0"
              : isAudioExt(entry.ext)
                ? "text-teal-400/90 shrink-0"
                : "text-gray-500 shrink-0"
          }
        />
        <span className="text-sm text-gray-200 truncate flex-1 min-w-0" title={entry.path}>
          {entry.name}
        </span>
        {!isDir && entry.ext && (
          <span className="text-[10px] uppercase tracking-wide text-gray-500 shrink-0">
            {entry.ext.replace(".", "")}
          </span>
        )}
        {!isDir && entry.size != null && (
          <span className="text-[11px] text-gray-500 tabular-nums shrink-0 w-16 text-right">
            {formatSize(entry.size)}
          </span>
        )}
        <button
          type="button"
          title={isDir ? "Delete folder and contents" : "Delete file"}
          onClick={() => onDelete(entry.path, entry.name, isDir)}
          disabled={deletingPath === entry.path}
          className="opacity-0 group-hover:opacity-100 focus:opacity-100 p-1 rounded text-red-400/80 hover:bg-red-900/40 hover:text-red-300 disabled:opacity-40 shrink-0"
        >
          <Trash2 size={13} />
        </button>
      </div>
      {isDir && open && entry.children && entry.children.length > 0 && (
        <div>
          {entry.children.map((child) => (
            <TreeNode
              key={child.path}
              entry={child}
              depth={depth + 1}
              onDelete={onDelete}
              deletingPath={deletingPath}
            />
          ))}
        </div>
      )}
      {isDir && open && entry.children && entry.children.length === 0 && (
        <p
          className="text-[11px] text-gray-600 py-0.5"
          style={{ paddingLeft: 28 + depth * 14 }}
        >
          Empty folder
        </p>
      )}
    </div>
  );
}

type Props = {
  requestId: number;
  title: string;
  open: boolean;
  onClose: () => void;
};

export default function StagingFilesViewer({ requestId, title, open, onClose }: Props) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const queryKey = useMemo(() => ["admin-staging-files", requestId] as const, [requestId]);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey,
    queryFn: async () => {
      const { data: body } = await api.get(`/admin/requests/${requestId}/staging-files`);
      return body as StagingTreeResponse;
    },
    enabled: open && requestId > 0,
    refetchOnWindowFocus: false,
  });

  const deleteMutation = useMutation({
    mutationFn: async (path: string) => {
      const { data: body } = await api.delete(`/admin/requests/${requestId}/staging-files`, {
        data: { path },
      });
      return body as { ok: boolean; deleted: string; type: string };
    },
    onSuccess: (_data, path) => {
      toast(`Deleted ${path}`, "info");
      void queryClient.invalidateQueries({ queryKey });
    },
    onError: (err: any) => {
      toast(err.response?.data?.detail || "Delete failed", "error");
    },
  });

  const handleDelete = (path: string, name: string, isDir: boolean) => {
    const message = isDir
      ? `Delete this folder and all contents?\n\n"${name}" will be removed from this request's quarantine/staging folder — not the final library.`
      : `Delete "${name}" from staging?\n\nThis only removes the file from this request's quarantine/staging folder — not the final library.`;
    if (!window.confirm(message)) {
      return;
    }
    deleteMutation.mutate(path);
  };

  return (
    <Modal title={`Staging files — ${title}`} show={open} onClose={onClose} size="lg">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-gray-500 min-w-0 truncate" title={data?.staging_path}>
            {data?.staging_path || "Loading staging path…"}
          </p>
          <button
            type="button"
            onClick={() => void refetch()}
            disabled={isFetching}
            className="text-xs px-2 py-1 rounded-lg border border-gray-600 text-gray-300 hover:bg-gray-700/60 disabled:opacity-50"
          >
            Refresh
          </button>
        </div>
        <p className="text-xs text-gray-400">
          Remove redundant audio (e.g. keep mp3, delete m4a) before Manual Review or Continue
          pipeline. Deletes stay inside this request&apos;s staging folder only.
        </p>

        {isLoading && <p className="text-sm text-gray-500 py-6 text-center">Loading files…</p>}
        {error && (
          <p className="text-sm text-red-400 py-4 text-center">
            {(error as any)?.response?.data?.detail || "Could not load staging files"}
          </p>
        )}
        {data && !isLoading && (
          <>
            {data.truncated && (
              <p className="text-xs text-amber-400/90">
                Listing truncated at {data.entry_count} entries.
              </p>
            )}
            {data.entries.length === 0 ? (
              <p className="text-sm text-gray-500 py-6 text-center">Staging folder is empty</p>
            ) : (
              <div className="border border-gray-700 rounded-xl bg-gray-900/50 max-h-[55vh] overflow-y-auto py-1">
                {data.entries.map((entry) => (
                  <TreeNode
                    key={entry.path}
                    entry={entry}
                    depth={0}
                    onDelete={handleDelete}
                    deletingPath={deleteMutation.isPending ? deleteMutation.variables ?? null : null}
                  />
                ))}
              </div>
            )}
            <p className="text-[11px] text-gray-600">
              {data.entry_count} item{data.entry_count === 1 ? "" : "s"}
              {data.root_name ? ` · ${data.root_name}` : ""}
            </p>
          </>
        )}
      </div>
    </Modal>
  );
}
