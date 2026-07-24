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

type ChosenMeta = {
  title?: string;
  subtitle?: string;
  author?: string;
  narrator?: string;
  series?: string;
  sequence?: string;
  year?: string;
  asin?: string;
  isbn?: string;
  publisher?: string;
  genre?: string;
  language?: string;
  summary?: string;
  cover_url?: string;
  [key: string]: unknown;
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
  publisher?: string;
  language?: string;
  duration_minutes?: number | null;
  summary?: string;
  recommended_edit_mode?: string;
  allowed_edit_modes?: string[];
  chosen_metadata?: ChosenMeta;
  chosen_metadata_by_mode?: Record<string, ChosenMeta>;
  duration?: {
    status?: string;
    diff_percent?: number | null;
    local_minutes?: number | null;
    audible_minutes?: number | null;
  };
  [key: string]: unknown;
};

const STEPS: { id: WizardStep; label: string; icon: typeof Files }[] = [
  { id: "files", label: "Files", icon: Files },
  { id: "metadata", label: "Metadata", icon: Tags },
  { id: "pipeline", label: "Run pipeline", icon: Play },
];

/** Mirror LibraForge Manual Review compare table fields. */
const COMPARE_FIELDS: { label: string; key: string }[] = [
  { label: "Title", key: "title" },
  { label: "Subtitle", key: "subtitle" },
  { label: "Author", key: "author" },
  { label: "Narrator", key: "narrator" },
  { label: "Series", key: "series" },
  { label: "Sequence", key: "sequence" },
  { label: "Year", key: "year" },
  { label: "ASIN", key: "asin" },
  { label: "ISBN", key: "isbn" },
  { label: "Publisher", key: "publisher" },
  { label: "Language", key: "language" },
  { label: "Genre", key: "genre" },
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

function chosenFor(result: SearchResult, mode?: string): ChosenMeta {
  const preferred = mode || result.recommended_edit_mode || "full";
  const byMode = result.chosen_metadata_by_mode?.[preferred];
  if (byMode && typeof byMode === "object") return byMode;
  if (result.chosen_metadata && typeof result.chosen_metadata === "object") {
    return result.chosen_metadata;
  }
  return {
    title: result.title || "",
    subtitle: result.subtitle || "",
    author: authorLine(result),
    narrator: narratorLine(result),
    series: result.series || "",
    sequence: result.sequence != null ? String(result.sequence) : "",
    year: result.year || "",
    asin: result.asin || "",
    publisher: result.publisher || "",
    language: result.language || "",
    summary: result.summary || "",
    cover_url: result.cover_url || "",
  };
}

function fieldStr(value: unknown): string {
  if (value == null) return "";
  if (Array.isArray(value)) return value.map(String).filter(Boolean).join(", ");
  return String(value).trim();
}

function formatMinutes(v: unknown): string {
  if (v == null || v === "") return "";
  const n = Number(v);
  if (Number.isNaN(n)) return "";
  return `${n.toFixed(1)} min`;
}

function localField(
  metadata: Record<string, unknown> | undefined,
  clues: Clues | undefined,
  key: string,
): string {
  const fromMeta = fieldStr(metadata?.[key]);
  if (fromMeta) return fromMeta;
  if (key === "author") return fieldStr(clues?.author);
  if (key === "title") return fieldStr(clues?.title);
  if (key === "narrator") return fieldStr(clues?.narrator);
  if (key === "series") return fieldStr(clues?.series);
  if (key === "sequence") return fieldStr(clues?.sequence);
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
          // Prefer full so LibraForge can embed cover (writers gate on edit_mode==full).
          edit_mode: "full",
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
  const currentCover = fieldStr(
    review?.metadata?.cover_url || (review?.clues as { cover_url?: string } | undefined)?.cover_url,
  );

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
                  <ul className="space-y-3 max-h-[48vh] overflow-y-auto pr-0.5">
                    {results.map((r, idx) => {
                      const key = r.asin || r.title || `r-${idx}`;
                      const active = selectedAsin === key;
                      const mode = r.recommended_edit_mode || "full";
                      const chosen = chosenFor(r, mode);
                      const dur = r.duration || {};
                      const localDur = formatMinutes(
                        review.metadata?.local_duration_minutes ?? dur.local_minutes,
                      );
                      const audibleDurRaw = r.duration_minutes ?? dur.audible_minutes;
                      let audibleDur = formatMinutes(audibleDurRaw);
                      if (audibleDur && dur.status) {
                        const pct =
                          dur.diff_percent || dur.diff_percent === 0
                            ? ` · ${dur.diff_percent}%`
                            : "";
                        audibleDur += ` (${dur.status}${pct})`;
                      }
                      const summary = fieldStr(chosen.summary || r.summary);
                      const matchCover = fieldStr(chosen.cover_url || r.cover_url);
                      const changedRows = COMPARE_FIELDS.map(({ label, key: fieldKey }) => {
                        const current = localField(review.metadata, review.clues, fieldKey);
                        let willWrite = fieldStr(chosen[fieldKey]);
                        if (!willWrite && fieldKey === "author") willWrite = authorLine(r);
                        if (!willWrite && fieldKey === "narrator") willWrite = narratorLine(r);
                        if (!willWrite && fieldKey === "title") willWrite = fieldStr(r.title);
                        if (!willWrite && fieldKey === "subtitle") willWrite = fieldStr(r.subtitle);
                        if (!willWrite && fieldKey === "series") willWrite = fieldStr(r.series);
                        if (!willWrite && fieldKey === "sequence") {
                          willWrite = r.sequence != null ? String(r.sequence) : "";
                        }
                        if (!willWrite && fieldKey === "year") willWrite = fieldStr(r.year);
                        if (!willWrite && fieldKey === "asin") willWrite = fieldStr(r.asin);
                        if (!willWrite && fieldKey === "publisher") willWrite = fieldStr(r.publisher);
                        if (!willWrite && fieldKey === "language") willWrite = fieldStr(r.language);
                        const changed = Boolean(willWrite && willWrite !== current);
                        return { label, current, willWrite, changed };
                      }).filter((row) => row.current || row.willWrite);

                      return (
                        <li key={key}>
                          <button
                            type="button"
                            onClick={() => setSelectedAsin(key)}
                            className={`w-full text-left p-3 rounded-xl border transition-colors ${
                              active
                                ? "border-teal-600/70 bg-teal-900/25"
                                : "border-gray-700 bg-gray-900/40 hover:border-gray-600"
                            }`}
                          >
                            <div className="flex gap-3">
                              <div className="w-14 h-14 sm:w-16 sm:h-16 rounded-lg overflow-hidden bg-gray-800 shrink-0 border border-gray-700">
                                {matchCover ? (
                                  <img
                                    src={matchCover}
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
                                  <div className="min-w-0">
                                    <p className="text-sm font-medium text-gray-100 truncate">
                                      {r.title || chosen.title || "Untitled"}
                                    </p>
                                    {fieldStr(r.subtitle || chosen.subtitle) && (
                                      <p className="text-[11px] text-gray-500 truncate">
                                        {fieldStr(r.subtitle || chosen.subtitle)}
                                      </p>
                                    )}
                                  </div>
                                  <span className="text-[11px] tabular-nums text-teal-300/90 shrink-0">
                                    {scoreLabel(r.score)}
                                  </span>
                                </div>
                                <div className="mt-1.5 flex flex-wrap gap-1.5 text-[10px] text-gray-400">
                                  {mode && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700">
                                      {mode === "full" ? "Full metadata" : "Series only"}
                                    </span>
                                  )}
                                  {r.asin && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700 font-mono">
                                      {r.asin}
                                    </span>
                                  )}
                                  {(r.series || chosen.series) && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700 truncate max-w-[12rem]">
                                      {r.series || chosen.series}
                                      {(r.sequence ?? chosen.sequence)
                                        ? ` #${r.sequence ?? chosen.sequence}`
                                        : ""}
                                    </span>
                                  )}
                                  {audibleDur && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700">
                                      {audibleDur}
                                    </span>
                                  )}
                                </div>
                                <p className="text-xs text-gray-400 truncate mt-1">
                                  {[authorLine(r) || chosen.author, narratorLine(r) || chosen.narrator]
                                    .filter(Boolean)
                                    .join(" · ")}
                                </p>
                              </div>
                            </div>

                            {(currentCover || matchCover) && (
                              <div className="mt-2.5 grid grid-cols-2 gap-2">
                                <div className="rounded-lg border border-gray-700/80 bg-gray-950/40 p-1.5">
                                  <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                                    Current
                                  </p>
                                  <div className="aspect-square max-h-20 mx-auto rounded overflow-hidden bg-gray-800">
                                    {currentCover ? (
                                      <img
                                        src={currentCover}
                                        alt=""
                                        className="w-full h-full object-cover"
                                        loading="lazy"
                                      />
                                    ) : (
                                      <div className="w-full h-full flex items-center justify-center text-[10px] text-gray-600">
                                        No cover
                                      </div>
                                    )}
                                  </div>
                                </div>
                                <div className="rounded-lg border border-gray-700/80 bg-gray-950/40 p-1.5">
                                  <p className="text-[10px] uppercase tracking-wide text-gray-500 mb-1">
                                    Match
                                  </p>
                                  <div className="aspect-square max-h-20 mx-auto rounded overflow-hidden bg-gray-800">
                                    {matchCover ? (
                                      <img
                                        src={matchCover}
                                        alt=""
                                        className="w-full h-full object-cover"
                                        loading="lazy"
                                      />
                                    ) : (
                                      <div className="w-full h-full flex items-center justify-center text-[10px] text-gray-600">
                                        No cover
                                      </div>
                                    )}
                                  </div>
                                </div>
                              </div>
                            )}

                            {changedRows.length > 0 && (
                              <div className="mt-2.5 overflow-x-auto rounded-lg border border-gray-700/70">
                                <table className="w-full text-[11px] text-left">
                                  <thead>
                                    <tr className="text-gray-500 border-b border-gray-700/70">
                                      <th className="px-2 py-1.5 font-medium">Field</th>
                                      <th className="px-2 py-1.5 font-medium">Current</th>
                                      <th className="px-2 py-1.5 font-medium">Will write</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {changedRows.map((row) => (
                                      <tr
                                        key={row.label}
                                        className={
                                          row.changed
                                            ? "bg-teal-950/20 text-gray-200"
                                            : "text-gray-400"
                                        }
                                      >
                                        <th className="px-2 py-1 font-medium text-gray-500 whitespace-nowrap">
                                          {row.label}
                                        </th>
                                        <td className="px-2 py-1 max-w-[8rem] truncate" title={row.current}>
                                          {row.current || "—"}
                                        </td>
                                        <td
                                          className="px-2 py-1 max-w-[8rem] truncate"
                                          title={row.willWrite}
                                        >
                                          {row.willWrite || "—"}
                                        </td>
                                      </tr>
                                    ))}
                                    {(localDur || audibleDur) && (
                                      <tr className="text-gray-400 border-t border-gray-800">
                                        <th className="px-2 py-1 font-medium text-gray-500">
                                          Duration
                                        </th>
                                        <td className="px-2 py-1">{localDur || "—"}</td>
                                        <td className="px-2 py-1">{audibleDur || "—"}</td>
                                      </tr>
                                    )}
                                  </tbody>
                                </table>
                              </div>
                            )}

                            {summary && (
                              <details
                                className="mt-2 text-[11px] text-gray-500"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <summary className="cursor-pointer text-gray-400 hover:text-gray-300">
                                  Summary
                                </summary>
                                <p className="mt-1 leading-relaxed text-gray-400 whitespace-pre-wrap">
                                  {summary}
                                </p>
                              </details>
                            )}
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
