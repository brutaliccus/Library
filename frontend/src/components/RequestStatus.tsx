import {
  Clock,
  CloudUpload,
  Download,
  ArrowRightLeft,
  FolderSync,
  Search,
  CheckCircle2,
  XCircle,
} from "lucide-react";

const STATUS_CONFIG: Record<
  string,
  { label: string; color: string; icon: typeof Clock }
> = {
  pending: { label: "Pending", color: "text-yellow-400 bg-yellow-900/30", icon: Clock },
  sent_to_rd: { label: "Sent to Real-Debrid", color: "text-blue-400 bg-blue-900/30", icon: CloudUpload },
  downloading_rd: { label: "Downloading (RD)", color: "text-indigo-400 bg-indigo-900/30", icon: Download },
  transferring: { label: "Transferring to Library", color: "text-purple-400 bg-purple-900/30", icon: ArrowRightLeft },
  organizing: { label: "Organizing Files", color: "text-violet-400 bg-violet-900/30", icon: FolderSync },
  matching: { label: "Matching Metadata", color: "text-fuchsia-400 bg-fuchsia-900/30", icon: Search },
  completed: { label: "Completed", color: "text-green-400 bg-green-900/30", icon: CheckCircle2 },
  failed: { label: "Failed", color: "text-red-400 bg-red-900/30", icon: XCircle },
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
