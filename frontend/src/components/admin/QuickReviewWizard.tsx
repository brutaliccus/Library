import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  ExternalLink,
  Files,
  Loader2,
  Play,
  Search,
  Tags,
} from "lucide-react";
import api from "../../api/client";
import Modal from "../Modal";
import { useToast } from "../../contexts/ToastContext";
import { StagingFilesPanel } from "./StagingFilesViewer";

type WizardStep = "files" | "metadata" | "pipeline";

type Clues = {
  query: string;
  title: string;
  author: string;
  series: string;
  sequence: string;
  narrator: string;
};

type QuickReviewLoad = {
  request_id: number;
  title: string;
  author: string | null;
  status: string;
  quarantine_reason: string | null;
  staging_path: string;
  manual_review_url: string | null;
  targets: {
    relative_path: string;
    path: string;
    display_name: string;
    file_count: number;
    is_grouped: boolean;
  }[];
  selected_relative_path: string;
  target_path: string;
  source_path: string;
  is_grouped: boolean;
  file_count: number;
  queries: string[];
  clues: Clues;
  metadata: Record<string, unknown>;
  provider_hint: string | null;
  already_applied: boolean;
};

type SearchResult = {
  asin?: string;
  title?: string;
  subtitle?: string;
  authors?: string[];
  narrators?: string[];
  series?: string;
  sequence?: string;
  score?: number | null;
  cover_url?: string;
  year?: string;
  recommended_edit_mode?: string;
  allowed_edit_modes?: string[];
  [key: string]: unknown;
};

const STEPS: { id: WizardStep; label: string; icon: typeof Files }[] = [
  { id: "files", label: "Files", icon: Files },
  { id: "metadata", label: "Metadata", icon: Tags },
  { id: "pipeline", label: "Run pipeline", icon: Play },
];

function scoreLabel(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "—";
  return score <= 1 ? `${Math.round(score * 100)}%` : String(Math.round(score));
}

function authorLine(r: SearchResult): string {
  if (Array.isArray(r.authors) && r.authors.length) return r.authors.join(", ");
  return "";
}

function narratorLine(r: SearchResult): string {
  if (Array.isArray(r.narrators) && r.narrators.length) return r.narrators.join(", ");
  return "";
}

type Props = {
  requestId: number;
  title: string;
  open: boolean;
  onClose: () => void;
  manualReviewUrl?: string | null;
};

export default function QuickReviewWizard({
  requestId,
  title,
  open,
  onClose,
  manualReviewUrl,
}: Props) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<WizardStep>("files");
  const [relativePath, setRelativePath] = useState("");
  const [clues, setClues] = useState<Clues>({
    query: "",
    title: "",
    author: "",
    series: "",
    sequence: "",
    narrator: "",
  });
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selectedAsin, setSelectedAsin] = useState<string | null>(null);
  const [metadataApplied, setMetadataApplied] = useState(false);

  const loadKey = useMemo(
    () => ["admin-quick-review", requestId, relativePath] as const,
    [requestId, relativePath],
  );

  const {
    data: review,
    isLoading: loadLoading,
    error: loadError,
    refetch: refetchReview,
  } = useQuery({
    queryKey: loadKey,
    queryFn: async () => {
      const params = relativePath ? { relative_path: relativePath } : undefined;
      const { data } = await api.get(`/admin/requests/${requestId}/quick-review`, { params });
      return data as QuickReviewLoad;
    },
    enabled: open && requestId > 0 && step !== "files",
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (!open) {
      setStep("files");
      setRelativePath("");
      setResults([]);
      setSelectedAsin(null);
      setMetadataApplied(false);
    }
  }, [open]);

  useEffect(() => {
    if (!review) return;
    setClues({
      query: review.clues?.query || review.queries?.[0] || "",
      title: review.clues?.title || "",
      author: review.clues?.author || "",
      series: review.clues?.series || "",
      sequence: review.clues?.sequence || "",
      narrator: review.clues?.narrator || "",
    });
    if (review.already_applied) setMetadataApplied(true);
    if (!relativePath && review.selected_relative_path) {
      setRelativePath(review.selected_relative_path);
    }
  }, [review, relativePath]);

  const searchMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post(`/admin/requests/${requestId}/quick-review/search`, {
        query: clues.query,
        title: clues.title,
        author: clues.author,
        series: clues.series,
        sequence: clues.sequence,
        narrator: clues.narrator,
        limit: 12,
      });
      return data as { results: SearchResult[]; queries: string[] };
    },
    onSuccess: (data) => {
      setResults(data.results || []);
      setSelectedAsin(null);
      if (!(data.results || []).length) toast("No metadata matches found", "info");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Search failed", "error"),
  });

  const applyMutation = useMutation({
    mutationFn: async (selected: SearchResult) => {
      const { data } = await api.post(
        `/admin/requests/${requestId}/quick-review/apply`,
        {
          relative_path: relativePath,
          selected_result: selected,
          edit_mode: selected.recommended_edit_mode || "full",
          replace_cover: true,
        },
        { timeout: 320_000 },
      );
      return data;
    },
    onSuccess: () => {
      setMetadataApplied(true);
      toast("Metadata applied to staging", "success");
      void queryClient.invalidateQueries({ queryKey: loadKey });
      void refetchReview();
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Apply failed", "error"),
  });

  const continueMutation = useMutation({
    mutationFn: () => api.post(`/admin/download-requests/${requestId}/continue-forge`),
    onSuccess: (res: any) => {
      void queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
      toast(res?.data?.message || "Continuing pipeline", "success");
      onClose();
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Continue failed", "error"),
  });

  const forgeUrl = review?.manual_review_url || manualReviewUrl || null;
  const selected = results.find((r) => (r.asin || r.title) === selectedAsin) || null;
  const stepIndex = STEPS.findIndex((s) => s.id === step);

  return (
    <Modal title={`Quick review — ${title}`} show={open} onClose={onClose} size="xl">
      <div className="space-y-4">
        <nav aria-label="Quick review steps" className="flex flex-wrap items-center gap-1 sm:gap-2">
          {STEPS.map((s, i) => {
            const Icon = s.icon;
            const active = s.id === step;
            const done = i < stepIndex;
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => {
                  if (i <= stepIndex || (s.id === "metadata" && step === "pipeline")) setStep(s.id);
                }}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                  active
                    ? "border-teal-600/60 bg-teal-900/30 text-teal-200"
                    : done
                      ? "border-gray-600 text-gray-300 hover:bg-gray-700/40"
                      : "border-gray-700 text-gray-500"
                }`}
              >
                {done ? <Check size={12} /> : <Icon size={12} />}
                <span>
                  {i + 1}. {s.label}
                </span>
              </button>
            );
          })}
        </nav>

        {forgeUrl && (
          <div className="flex justify-end">
            <a
              href={forgeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs text-amber-300/90 hover:text-amber-200"
            >
              <ExternalLink size={12} />
              Open in LibraForge
            </a>
          </div>
        )}

        {step === "files" && (
          <div className="space-y-4">
            <StagingFilesPanel requestId={requestId} compact />
            <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={onClose}
                className="px-3 py-2 text-sm rounded-lg border border-gray-600 text-gray-300 hover:bg-gray-700/50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => setStep("metadata")}
                className="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600"
              >
                Files ready — next
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        )}

        {step === "metadata" && (
          <div className="space-y-4">
            {loadLoading && (
              <p className="text-sm text-gray-500 py-8 text-center inline-flex items-center justify-center gap-2 w-full">
                <Loader2 size={16} className="animate-spin" /> Loading clues…
              </p>
            )}
            {loadError && (
              <p className="text-sm text-red-400 text-center">
                {(loadError as any)?.response?.data?.detail || "Could not load metadata clues"}
              </p>
            )}
            {review && !loadLoading && (
              <>
                <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
                  <span
                    className="truncate max-w-full font-mono text-[11px] text-gray-500"
                    title={review.target_path}
                  >
                    {review.target_path}
                  </span>
                  {(review.is_grouped || review.file_count > 1) && (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-amber-900/40 text-amber-200 border border-amber-700/40">
                      Multi-file · {review.file_count}
                    </span>
                  )}
                  {review.provider_hint && (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-gray-700/80 text-gray-300 border border-gray-600">
                      Hint: {review.provider_hint}
                    </span>
                  )}
                  {metadataApplied && (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-teal-900/40 text-teal-200 border border-teal-700/40">
                      <Check size={11} /> Applied
                    </span>
                  )}
                </div>

                {review.targets.length > 1 && (
                  <label className="block text-xs text-gray-400">
                    Target book folder
                    <select
                      className="mt-1 w-full bg-gray-900 border border-gray-600 rounded-lg px-2.5 py-2 text-sm text-gray-100"
                      value={relativePath}
                      onChange={(e) => {
                        setRelativePath(e.target.value);
                        setResults([]);
                        setSelectedAsin(null);
                        setMetadataApplied(false);
                      }}
                    >
                      {review.targets.map((t) => (
                        <option key={t.path} value={t.relative_path}>
                          {t.display_name}
                          {t.is_grouped ? ` (${t.file_count} files)` : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                )}

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                  {(
                    [
                      ["query", "Search query", true],
                      ["title", "Title", false],
                      ["author", "Author", false],
                      ["series", "Series", false],
                      ["sequence", "Sequence", false],
                      ["narrator", "Narrator", false],
                    ] as const
                  ).map(([key, label, full]) => (
                    <label
                      key={key}
                      className={`block text-xs text-gray-400 ${full ? "sm:col-span-2" : ""}`}
                    >
                      {label}
                      <input
                        type="text"
                        value={clues[key]}
                        onChange={(e) => setClues((c) => ({ ...c, [key]: e.target.value }))}
                        className="mt-1 w-full bg-gray-900 border border-gray-600 rounded-lg px-2.5 py-2 text-sm text-gray-100"
                      />
                    </label>
                  ))}
                </div>

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => searchMutation.mutate()}
                    disabled={searchMutation.isPending || !clues.query.trim()}
                    className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600 disabled:opacity-50"
                  >
                    {searchMutation.isPending ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : (
                      <Search size={14} />
                    )}
                    Search
                  </button>
                  {selected && (
                    <button
                      type="button"
                      onClick={() => applyMutation.mutate(selected)}
                      disabled={applyMutation.isPending}
                      className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-teal-600/60 text-teal-200 hover:bg-teal-900/30 disabled:opacity-50"
                    >
                      {applyMutation.isPending ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <Check size={14} />
                      )}
                      Apply metadata
                    </button>
                  )}
                </div>

                {results.length > 0 && (
                  <ul className="space-y-2 max-h-[36vh] overflow-y-auto">
                    {results.map((r, idx) => {
                      const key = r.asin || r.title || `r-${idx}`;
                      const active = selectedAsin === key;
                      return (
                        <li key={key}>
                          <button
                            type="button"
                            onClick={() => setSelectedAsin(key)}
                            className={`w-full text-left flex gap-3 p-2.5 rounded-xl border transition-colors ${
                              active
                                ? "border-teal-600/70 bg-teal-900/25"
                                : "border-gray-700 bg-gray-900/40 hover:border-gray-600"
                            }`}
                          >
                            <div className="w-12 h-12 sm:w-14 sm:h-14 rounded-lg overflow-hidden bg-gray-800 shrink-0 border border-gray-700">
                              {r.cover_url ? (
                                <img
                                  src={r.cover_url}
                                  alt=""
                                  className="w-full h-full object-cover"
                                  loading="lazy"
                                />
                              ) : (
                                <div className="w-full h-full flex items-center justify-center text-gray-600 text-[10px]">
                                  —
                                </div>
                              )}
                            </div>
                            <div className="min-w-0 flex-1">
                              <div className="flex items-start justify-between gap-2">
                                <p className="text-sm font-medium text-gray-100 truncate">
                                  {r.title || "Untitled"}
                                </p>
                                <span className="text-[11px] tabular-nums text-teal-300/90 shrink-0">
                                  {scoreLabel(r.score)}
                                </span>
                              </div>
                              {authorLine(r) && (
                                <p className="text-xs text-gray-400 truncate mt-0.5">{authorLine(r)}</p>
                              )}
                              {narratorLine(r) && (
                                <p className="text-[11px] text-gray-500 truncate">
                                  Narrated by {narratorLine(r)}
                                </p>
                              )}
                              {(r.series || r.asin) && (
                                <p className="text-[11px] text-gray-600 truncate mt-0.5">
                                  {[
                                    r.series &&
                                      `Series: ${r.series}${r.sequence ? ` #${r.sequence}` : ""}`,
                                    r.asin,
                                  ]
                                    .filter(Boolean)
                                    .join(" · ")}
                                </p>
                              )}
                            </div>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </>
            )}

            <div className="flex flex-col-reverse sm:flex-row sm:justify-between gap-2 pt-1">
              <button
                type="button"
                onClick={() => setStep("files")}
                className="px-3 py-2 text-sm rounded-lg border border-gray-600 text-gray-300 hover:bg-gray-700/50"
              >
                Back
              </button>
              <div className="flex flex-col-reverse sm:flex-row gap-2">
                <button
                  type="button"
                  onClick={() => setStep("pipeline")}
                  className="px-3 py-2 text-sm rounded-lg border border-gray-600 text-gray-300 hover:bg-gray-700/50"
                >
                  {metadataApplied ? "Next" : "Skip — metadata already good"}
                </button>
                {metadataApplied && (
                  <button
                    type="button"
                    onClick={() => setStep("pipeline")}
                    className="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600"
                  >
                    Continue to pipeline
                    <ChevronRight size={14} />
                  </button>
                )}
              </div>
            </div>
          </div>
        )}

        {step === "pipeline" && (
          <div className="space-y-4">
            <div className="rounded-xl border border-gray-700 bg-gray-900/40 p-4 space-y-2">
              <p className="text-sm text-gray-200">
                Resume the existing pipeline from M4B convert → Folder Forge → finalize.
              </p>
              <p className="text-xs text-gray-500">
                Same path as Admin &quot;Continue pipeline&quot;. Progress appears on the request card.
              </p>
              {metadataApplied ? (
                <p className="text-xs text-teal-300/90 inline-flex items-center gap-1">
                  <Check size={12} /> Metadata write evidence present (or just applied).
                </p>
              ) : (
                <p className="text-xs text-amber-300/90">
                  Metadata was skipped — only continue if tags/cover are already correct in staging.
                </p>
              )}
            </div>
            <div className="flex flex-col-reverse sm:flex-row sm:justify-between gap-2">
              <button
                type="button"
                onClick={() => setStep("metadata")}
                className="px-3 py-2 text-sm rounded-lg border border-gray-600 text-gray-300 hover:bg-gray-700/50"
              >
                Back
              </button>
              <button
                type="button"
                onClick={() => continueMutation.mutate()}
                disabled={continueMutation.isPending}
                className="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600 disabled:opacity-50"
              >
                {continueMutation.isPending ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <Play size={14} />
                )}
                Run pipeline
              </button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
