/**
 * Calibre-Web-style metadata under a shelf cover:
 * bold title → gray author → quieter "Series Name (1)".
 * Lives in normal document flow so taller titles grow the row.
 */

export function formatSeriesLabel(
  name?: string | null,
  sequence?: string | number | null
): string {
  const n = (name || "").trim();
  if (!n) return "";
  const seq = String(sequence ?? "")
    .replace(/^#/, "")
    .trim();
  if (seq) return `${n} (${seq})`;
  return n;
}

interface Props {
  title: string;
  author?: string;
  seriesName?: string | null;
  sequence?: string | number | null;
  className?: string;
  titleClassName?: string;
  onTitleClick?: () => void;
  children?: React.ReactNode;
}

export default function ShelfCardMeta({
  title,
  author,
  seriesName,
  sequence,
  className = "",
  titleClassName = "",
  onTitleClick,
  children,
}: Props) {
  const series = formatSeriesLabel(seriesName, sequence);
  const authorText = (author || "").trim();

  return (
    <div className={`pt-1.5 px-0.5 pb-0.5 flex flex-col gap-0.5 min-w-0 ${className}`}>
      <h3
        className={`text-[11px] font-bold text-gray-100 leading-snug line-clamp-2 ${
          onTitleClick ? "cursor-pointer" : ""
        } ${titleClassName}`}
        title={title}
        onClick={onTitleClick}
      >
        {title}
      </h3>
      {authorText ? (
        <p className="text-[10px] text-gray-400 leading-snug truncate" title={authorText}>
          {authorText}
        </p>
      ) : null}
      {series ? (
        <p className="text-[10px] text-gray-500 leading-snug truncate" title={series}>
          {series}
        </p>
      ) : null}
      {children}
    </div>
  );
}
