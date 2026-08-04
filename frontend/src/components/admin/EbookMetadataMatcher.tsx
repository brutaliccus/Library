import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
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
  cover_url?: string;
};

type EbookReviewLoad = {
  request_id: number;
  title: string;
  author: string | null;
  status: string;
  quarantine_reason: string | null;
  staging_path: string;
  targets: {
    relative_path: string;
    path: string;
    display_name: string;
    file_count: number;
    is_grouped: boolean;
  }[];
  selected_relative_path: string;
  primary_ebook: string | null;
  queries: string[];
  clues: Clues;
  metadata: Record<string, unknown>;
  already_applied: boolean;
  provider: string;
};

type SearchResult = {
  id?: string;
  hardcover_id?: number | string;
  hardcover_slug?: string;
  title?: string;
  subtitle?: string;
  authors?: string[];
  series?: string;
  sequence?: string;
  score?: number | null;
  cover_url?: string;
  year?: string;
  isbn13?: string;
  isbn10?: string;
  summary?: string;
  publisher?: string;
  language?: string;
  info_link?: string;
  [key: string]: unknown;
};

const STEPS: { id: WizardStep; label: string; icon: typeof Files }[] = [
  { id: "files", label: "Files", icon: Files },
  { id: "metadata", label: "Metadata", icon: Tags },
  { id: "pipeline", label: "Continue", icon: Play },
];

const COMPARE_FIELDS: { label: string; key: string }[] = [
  { label: "Title", key: "title" },
  { label: "Subtitle", key: "subtitle" },
  { label: "Author", key: "author" },
  { label: "Series", key: "series" },
  { label: "Sequence", key: "sequence" },
  { label: "Year", key: "year" },
  { label: "ISBN-13", key: "isbn13" },
  { label: "ISBN-10", key: "isbn10" },
  { label: "Publisher", key: "publisher" },
  { label: "Language", key: "language" },
];

function fieldStr(value: unknown): string {
  if (value == null) return "";
  return String(value).trim();
}

function scoreLabel(score: number | null | undefined): string {
  if (score == null || Number.isNaN(score)) return "—";
  return score <= 1 ? `${Math.round(score * 100)}%` : String(Math.round(score));
}

function authorLine(r: SearchResult): string {
  if (Array.isArray(r.authors) && r.authors.length) return r.authors.join(", ");
  return "";
}

function resultKey(r: SearchResult, idx: number): string {
  return fieldStr(r.id) || fieldStr(r.hardcover_id) || fieldStr(r.title) || `r-${idx}`;
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
  if (key === "series") return fieldStr(clues?.series);
  if (key === "sequence") return fieldStr(clues?.sequence);
  return "";
}

type Props = {
  requestId: number;
  title: string;
  open: boolean;
  onClose: () => void;
};

export default function EbookMetadataMatcher({
  requestId,
  title,
  open,
  onClose,
}: Props) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [step, setStep] = useState<WizardStep>("files");
  const [clues, setClues] = useState<Clues>({
    query: "",
    title: "",
    author: "",
    series: "",
    sequence: "",
  });
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [metadataApplied, setMetadataApplied] = useState(false);

  const loadKey = useMemo(
    () => ["admin-ebook-review", requestId] as const,
    [requestId],
  );

  const {
    data: review,
    isLoading: loadLoading,
    error: loadError,
    refetch: refetchReview,
  } = useQuery({
    queryKey: loadKey,
    queryFn: async () => {
      const { data } = await api.get(`/admin/requests/${requestId}/ebook-review`);
      return data as EbookReviewLoad;
    },
    enabled: open && requestId > 0 && step !== "files",
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (!open) {
      setStep("files");
      setResults([]);
      setSelectedKey(null);
      setMetadataApplied(false);
      setClues({ query: "", title: "", author: "", series: "", sequence: "" });
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
      cover_url: review.clues?.cover_url || "",
    });
    if (review.already_applied) setMetadataApplied(true);
  }, [review]);

  const searchMutation = useMutation({
    mutationFn: async () => {
      const { data } = await api.post(`/admin/requests/${requestId}/ebook-review/search`, {
        query: clues.query,
        title: clues.title,
        author: clues.author,
        limit: 12,
      });
      return data as { results: SearchResult[]; queries: string[]; provider?: string };
    },
    onSuccess: (data) => {
      setResults(data.results || []);
      setSelectedKey(null);
      if (!(data.results || []).length) toast("No Hardcover matches found", "info");
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Search failed", "error"),
  });

  const applyMutation = useMutation({
    mutationFn: async (selected: SearchResult) => {
      const { data } = await api.post(
        `/admin/requests/${requestId}/ebook-review/apply`,
        { selected_result: selected },
        { timeout: 120_000 },
      );
      return data;
    },
    onSuccess: () => {
      setMetadataApplied(true);
      toast("Metadata applied to staging", "success");
      void queryClient.invalidateQueries({ queryKey: loadKey });
      void queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
      void refetchReview();
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Apply failed", "error"),
  });

  const continueMutation = useMutation({
    mutationFn: () =>
      api.post(`/admin/download-requests/${requestId}/continue-forge`, {
        resume_from: "folder",
      }),
    onSuccess: (res: any) => {
      void queryClient.invalidateQueries({ queryKey: ["admin-downloads"] });
      toast(res?.data?.message || "Continuing ebook pipeline", "success");
      onClose();
    },
    onError: (err: any) => toast(err.response?.data?.detail || "Continue failed", "error"),
  });

  const selected =
    results.find((r, idx) => resultKey(r, idx) === selectedKey) || null;
  const stepIndex = STEPS.findIndex((s) => s.id === step);
  const currentCover = fieldStr(
    review?.metadata?.cover_url || review?.clues?.cover_url || clues.cover_url,
  );

  const canJumpTo = (target: WizardStep, index: number) => {
    if (index <= stepIndex) return true;
    if (target === "metadata" && step !== "files") return true;
    if (target === "pipeline" && (metadataApplied || stepIndex >= 1)) return true;
    return false;
  };

  return (
    <Modal title={`Ebook metadata — ${title}`} show={open} onClose={onClose} size="xl">
      <div className="space-y-4">
        <nav
          aria-label="Ebook metadata steps"
          className="flex flex-wrap items-center gap-1 sm:gap-2"
        >
          {STEPS.map((s, i) => {
            const Icon = s.icon;
            const active = s.id === step;
            const done =
              i < stepIndex || (s.id === "metadata" && metadataApplied);
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => {
                  if (canJumpTo(s.id, i)) setStep(s.id);
                }}
                className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                  active
                    ? "border-teal-600/60 bg-teal-900/30 text-teal-200"
                    : done
                      ? "border-gray-600 text-gray-300 hover:bg-gray-700/40"
                      : "border-gray-700 text-gray-500"
                }`}
              >
                {done && !active ? <Check size={12} /> : <Icon size={12} />}
                <span>
                  {i + 1}. {s.label}
                </span>
              </button>
            );
          })}
        </nav>

        {step === "files" && (
          <div className="space-y-3">
            <p className="text-sm text-gray-400">
              Review staging files, then match metadata via Hardcover before continuing
              organize → Kavita.
            </p>
            <StagingFilesPanel requestId={requestId} compact />
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => setStep("metadata")}
                className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600"
              >
                <Tags size={14} />
                Match metadata
              </button>
            </div>
          </div>
        )}

        {step === "metadata" && (
          <div className="space-y-3">
            {loadLoading && (
              <div className="flex items-center gap-2 text-sm text-gray-400 py-6 justify-center">
                <Loader2 size={16} className="animate-spin" />
                Loading clues…
              </div>
            )}
            {loadError && (
              <p className="text-sm text-red-300">
                {(loadError as any)?.response?.data?.detail || "Failed to load review"}
              </p>
            )}
            {review && (
              <>
                {review.quarantine_reason && (
                  <p className="text-xs text-amber-300/90 bg-amber-950/30 border border-amber-800/40 rounded-lg px-3 py-2">
                    Quarantine: {review.quarantine_reason}
                  </p>
                )}
                {metadataApplied && (
                  <p className="text-xs text-teal-300/90 bg-teal-950/30 border border-teal-800/40 rounded-lg px-3 py-2">
                    Metadata applied
                    {review.primary_ebook ? ` to ${review.primary_ebook}` : ""}. Continue
                    the pipeline when ready.
                  </p>
                )}

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {(
                    [
                      ["query", "Search query", true],
                      ["title", "Title", false],
                      ["author", "Author", false],
                      ["series", "Series", false],
                      ["sequence", "Sequence", false],
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

                <div className="flex flex-wrap items-end gap-2">
                  <span className="inline-flex items-center px-2.5 py-2 text-xs rounded-lg border border-gray-700 bg-gray-900 text-gray-300">
                    Provider: Hardcover
                  </span>
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
                  {metadataApplied && (
                    <button
                      type="button"
                      onClick={() => setStep("pipeline")}
                      className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-gray-600 text-gray-200 hover:bg-gray-700/40"
                    >
                      <Play size={14} />
                      Continue pipeline
                    </button>
                  )}
                </div>

                {results.length > 0 && (
                  <ul className="space-y-3 max-h-[40vh] overflow-y-auto pr-0.5">
                    {results.map((r, idx) => {
                      const key = resultKey(r, idx);
                      const active = selectedKey === key;
                      const matchCover = fieldStr(r.cover_url);
                      const changedRows = COMPARE_FIELDS.map(({ label, key: fieldKey }) => {
                        const current = localField(review.metadata, review.clues, fieldKey);
                        let willWrite = fieldStr(r[fieldKey]);
                        if (!willWrite && fieldKey === "author") willWrite = authorLine(r);
                        if (!willWrite && fieldKey === "title") willWrite = fieldStr(r.title);
                        if (!willWrite && fieldKey === "subtitle") willWrite = fieldStr(r.subtitle);
                        if (!willWrite && fieldKey === "series") willWrite = fieldStr(r.series);
                        if (!willWrite && fieldKey === "sequence") {
                          willWrite = r.sequence != null ? String(r.sequence) : "";
                        }
                        const changed = Boolean(willWrite && willWrite !== current);
                        return { label, current, willWrite, changed };
                      }).filter((row) => row.current || row.willWrite);

                      return (
                        <li key={key}>
                          <button
                            type="button"
                            onClick={() => setSelectedKey(key)}
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
                                      {r.title || "Untitled"}
                                    </p>
                                    {fieldStr(r.subtitle) && (
                                      <p className="text-[11px] text-gray-500 truncate">
                                        {fieldStr(r.subtitle)}
                                      </p>
                                    )}
                                  </div>
                                  <span className="text-[11px] tabular-nums text-teal-300/90 shrink-0">
                                    {scoreLabel(r.score)}
                                  </span>
                                </div>
                                <div className="mt-1.5 flex flex-wrap gap-1.5 text-[10px] text-gray-400">
                                  <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700">
                                    Hardcover
                                  </span>
                                  {(r.isbn13 || r.isbn10) && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700 font-mono">
                                      {r.isbn13 || r.isbn10}
                                    </span>
                                  )}
                                  {r.series && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700 truncate max-w-[12rem]">
                                      {r.series}
                                      {r.sequence ? ` #${r.sequence}` : ""}
                                    </span>
                                  )}
                                  {r.year && (
                                    <span className="px-1.5 py-0.5 rounded bg-gray-800 border border-gray-700">
                                      {r.year}
                                    </span>
                                  )}
                                </div>
                                <p className="text-xs text-gray-400 truncate mt-1">
                                  {authorLine(r)}
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

                            {changedRows.length > 0 && active && (
                              <div className="mt-2.5 overflow-x-auto">
                                <table className="w-full text-[11px]">
                                  <thead>
                                    <tr className="text-gray-500 text-left">
                                      <th className="py-1 pr-2 font-medium">Field</th>
                                      <th className="py-1 pr-2 font-medium">Current</th>
                                      <th className="py-1 font-medium">Will write</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {changedRows.map((row) => (
                                      <tr
                                        key={row.label}
                                        className={
                                          row.changed ? "text-teal-200/90" : "text-gray-400"
                                        }
                                      >
                                        <td className="py-0.5 pr-2 whitespace-nowrap">
                                          {row.label}
                                        </td>
                                        <td className="py-0.5 pr-2 max-w-[10rem] truncate">
                                          {row.current || "—"}
                                        </td>
                                        <td className="py-0.5 max-w-[10rem] truncate">
                                          {row.willWrite || "—"}
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}

                            {active && fieldStr(r.summary) && (
                              <p className="mt-2 text-[11px] text-gray-500 line-clamp-3">
                                {fieldStr(r.summary)}
                              </p>
                            )}
                            {active && fieldStr(r.info_link) && (
                              <a
                                href={fieldStr(r.info_link)}
                                target="_blank"
                                rel="noopener noreferrer"
                                onClick={(e) => e.stopPropagation()}
                                className="mt-2 inline-flex items-center gap-1 text-[11px] text-amber-300/90 hover:text-amber-200"
                              >
                                <ExternalLink size={11} />
                                Open on Hardcover
                              </a>
                            )}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </>
            )}
          </div>
        )}

        {step === "pipeline" && (
          <div className="space-y-3">
            <p className="text-sm text-gray-300">
              {metadataApplied
                ? "Selected Hardcover metadata is saved. Continue to organize the ebook and scan Kavita."
                : "You can continue without a new match (uses request hints / auto-identify). Prefer applying a Hardcover match first when quarantine was caused by ambiguous metadata."}
            </p>
            <div className="rounded-lg border border-gray-700 bg-gray-900/40 px-3 py-2 text-xs text-gray-400">
              Next: Organize → Finalize (Kavita scan)
            </div>
            <div className="flex flex-wrap gap-2 justify-end">
              <button
                type="button"
                onClick={() => setStep("metadata")}
                className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-gray-600 text-gray-200 hover:bg-gray-700/40"
              >
                <Tags size={14} />
                Back to metadata
              </button>
              <button
                type="button"
                onClick={() => continueMutation.mutate()}
                disabled={continueMutation.isPending}
                className="inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg bg-teal-700/80 text-white hover:bg-teal-600 disabled:opacity-50"
              >
                {continueMutation.isPending ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <Play size={14} />
                )}
                Continue pipeline
              </button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
