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
} from "lucide-react";

const STATUS_CONFIG: Record<
  string,
  { label: string; color: string; icon: typeof Clock }
> = {
  pending: { label: "Pending", color: "text-yellow-400 bg-yellow-900/30", icon: Clock },
  sent_to_rd: { label: "Sent to Real-Debrid", color: "text-blue-400 bg-blue-900/30", icon: CloudUpload },
  downloading_rd: { label: "Downloading (RD)", color: "text-indigo-400 bg-indigo-900/30", icon: Download },
  transferring: { label: "Transferring to Library", color: "text-sky-400 bg-sky-900/30", icon: ArrowRightLeft },
  organizing: { label: "Organizing Files", color: "text-violet-400 bg-violet-900/30", icon: FolderSync },
  matching: { label: "Matching Metadata", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: Search },
  metadata_forge: { label: "Metadata Forge", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: Sparkles },
  m4b_convert: { label: "Converting M4B", color: "text-cyan-400 bg-cyan-900/30", icon: Disc3 },
  folder_forge: { label: "Folder Forge", color: "text-teal-400 bg-teal-900/30", icon: FolderSync },
  finalizing: { label: "Finalizing", color: "text-emerald-400 bg-emerald-900/30", icon: Library },
  quarantined: { label: "Needs Admin Review", color: "text-amber-400 bg-amber-900/30", icon: AlertTriangle },
  admin_rejected: { label: "Rejected by Admin", color: "text-red-400 bg-red-900/30", icon: Ban },
  completed: { label: "Completed", color: "text-green-400 bg-green-900/30", icon: CheckCircle2 },
  failed: { label: "Failed", color: "text-red-400 bg-red-900/30", icon: XCircle },
  cancelled: { label: "Cancelled", color: "text-gray-400 bg-gray-800/60", icon: Ban },
};

interface Props {
  status: string;
  detail?: string | null;
}

export default function RequestStatusBadge({ status, detail }: Props) {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.pending;
  const Icon = config.icon;

  return (
    <div className="flex items-center gap-2">
      <span
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${config.color}`}
      >
        <Icon size={13} />
        {config.label}
      </span>
      {detail && status !== "completed" && (
        <span className="text-xs text-gray-500 truncate max-w-[200px]">
          {detail}
        </span>
      )}
    </div>
  );
}
