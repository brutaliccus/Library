import {
  Clock,
  CloudUpload,
  Download,
  ArrowRightLeft,
  FolderSync,
  Search,
  CheckCircle2,
  XCircle,
  Ban,
  AlertTriangle,
  Disc3,
  Library,
  Sparkles,
  Headphones,
} from "lucide-react";

const STATUS_CONFIG: Record<
  string,
  { label: string; color: string; icon: typeof Clock }
> = {
  pending: { label: "Pending", color: "text-yellow-400 bg-yellow-900/30", icon: Clock },
  sent_to_rd: { label: "Sent to Debrid", color: "text-blue-400 bg-blue-900/30", icon: CloudUpload },
  downloading_rd: { label: "Downloading (Debrid)", color: "text-indigo-400 bg-indigo-900/30", icon: Download },
  transferring: { label: "Transferring to Library", color: "text-sky-400 bg-sky-900/30", icon: ArrowRightLeft },
  organizing: { label: "Organizing Files", color: "text-violet-400 bg-violet-900/30", icon: FolderSync },
  matching: { label: "Matching Metadata", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: Search },
  metadata_forge: { label: "Metadata Forge", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: Sparkles },
  m4b_convert: { label: "Converting M4B", color: "text-cyan-400 bg-cyan-900/30", icon: Disc3 },
  chapter_forge: { label: "Chapter Forge", color: "text-indigo-400 bg-indigo-900/30", icon: Library },
  folder_forge: { label: "Folder Forge", color: "text-teal-400 bg-teal-900/30", icon: FolderSync },
  finalizing: { label: "Finalizing", color: "text-emerald-400 bg-emerald-900/30", icon: Library },
  quarantined: { label: "Needs Admin Review", color: "text-amber-400 bg-amber-900/30", icon: AlertTriangle },
  admin_rejected: { label: "Rejected by Admin", color: "text-red-400 bg-red-900/30", icon: Ban },
  completed: { label: "Completed", color: "text-green-400 bg-green-900/30", icon: CheckCircle2 },
  failed: { label: "Failed", color: "text-red-400 bg-red-900/30", icon: XCircle },
  cancelled: { label: "Cancelled", color: "text-gray-400 bg-gray-800/60", icon: Ban },
};

/** Listener-facing stages for the Downloads page (hide forge vocabulary). */
const PLAIN_STATUS: Record<
  string,
  { label: string; color: string; icon: typeof Clock }
> = {
  pending: { label: "Downloading", color: "text-yellow-400 bg-yellow-900/30", icon: Download },
  sent_to_rd: { label: "Downloading", color: "text-blue-400 bg-blue-900/30", icon: Download },
  downloading_rd: { label: "Downloading", color: "text-indigo-400 bg-indigo-900/30", icon: Download },
  transferring: { label: "Preparing", color: "text-sky-400 bg-sky-900/30", icon: FolderSync },
  organizing: { label: "Preparing", color: "text-violet-400 bg-violet-900/30", icon: FolderSync },
  matching: { label: "Preparing", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: FolderSync },
  metadata_forge: { label: "Preparing", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: FolderSync },
  m4b_convert: { label: "Almost ready", color: "text-cyan-400 bg-cyan-900/30", icon: Headphones },
  chapter_forge: { label: "Almost ready", color: "text-indigo-400 bg-indigo-900/30", icon: Headphones },
  folder_forge: { label: "Almost ready", color: "text-teal-400 bg-teal-900/30", icon: Headphones },
  finalizing: { label: "Almost ready", color: "text-emerald-400 bg-emerald-900/30", icon: Headphones },
  quarantined: { label: "Waiting on library admin", color: "text-amber-400 bg-amber-900/30", icon: AlertTriangle },
  admin_rejected: { label: "Rejected by admin", color: "text-red-400 bg-red-900/30", icon: Ban },
  completed: { label: "Ready to listen", color: "text-green-400 bg-green-900/30", icon: CheckCircle2 },
  failed: { label: "Failed", color: "text-red-400 bg-red-900/30", icon: XCircle },
  cancelled: { label: "Cancelled", color: "text-gray-400 bg-gray-800/60", icon: Ban },
};

interface Props {
  status: string;
  detail?: string | null;
  /** When true (default), show listener-friendly labels. Admin pages pass false. */
  plainLanguage?: boolean;
  /** When ``ebook``, completed status shows "Ready to read" instead of "Ready to listen". */
  mediaType?: string | null;
}

function isM4bQueued(status: string, detail?: string | null): boolean {
  if (status !== "m4b_convert" || !detail) return false;
  return /waiting in m4b queue|queued for m4b/i.test(detail);
}

export default function RequestStatusBadge({
  status,
  detail,
  plainLanguage = true,
  mediaType,
}: Props) {
  const tech = STATUS_CONFIG[status] || STATUS_CONFIG.pending;
  const plain = PLAIN_STATUS[status] || PLAIN_STATUS.pending;
  const queued = isM4bQueued(status, detail);

  let label: string;
  let color: string;
  let Icon: typeof Clock;

  if (plainLanguage) {
    if (queued) {
      label = "Almost ready";
      color = "text-amber-300 bg-amber-900/30";
      Icon = Headphones;
    } else {
      label = plain.label;
      if (status === "completed" && mediaType === "ebook") {
        label = "Ready to read";
      }
      color = plain.color;
      Icon = plain.icon;
    }
  } else if (queued) {
    label = "Queued for M4B";
    color = "text-amber-300 bg-amber-900/30";
    Icon = tech.icon;
  } else {
    label = tech.label;
    color = tech.color;
    Icon = tech.icon;
  }

  const showDetail =
    detail &&
    status !== "completed" &&
    (!plainLanguage || status === "failed" || status === "quarantined");

  return (
    <div className="flex items-center gap-2">
      <span
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${color}`}
      >
        <Icon size={13} />
        {label}
      </span>
      {showDetail && (
        <span className="text-xs text-gray-500 truncate max-w-[200px]">
          {detail}
        </span>
      )}
    </div>
  );
}
