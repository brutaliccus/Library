import {
  useEffect,
  useState,
  type ImgHTMLAttributes,
  type ReactNode,
  type SyntheticEvent,
} from "react";
import { toAbsoluteUrl } from "../api/instanceUrl";

/**
 * Book cover &lt;img&gt; that resolves root-relative /api/… URLs against the
 * Library server on Capacitor (WebView origin is https://localhost).
 *
 * Optional fallbackSrc (string or list) is tried when earlier URLs fail —
 * e.g. Open Library -L.jpg 404 while -M.jpg / Hardcover art still works.
 */
type Props = Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  src?: string | null;
  /** Tried if `src` errors. Deduped against src. */
  fallbackSrc?: string | null | Array<string | null | undefined>;
  fallback?: ReactNode;
};

function buildChain(
  src?: string | null,
  fallbackSrc?: string | null | Array<string | null | undefined>
): string[] {
  const out: string[] = [];
  const push = (u?: string | null) => {
    const v = (u || "").trim();
    if (v && !out.includes(v)) out.push(v);
  };
  push(src);
  if (Array.isArray(fallbackSrc)) {
    for (const u of fallbackSrc) push(u);
  } else {
    push(fallbackSrc);
  }
  return out;
}

export default function CoverImage({
  src,
  fallbackSrc,
  fallback = null,
  alt = "",
  onError,
  ...rest
}: Props) {
  const chain = buildChain(src, fallbackSrc);
  const [index, setIndex] = useState(0);
  const chainKey = chain.join("\0");

  useEffect(() => {
    setIndex(0);
  }, [chainKey]);

  if (chain.length === 0 || index >= chain.length) {
    return <>{fallback}</>;
  }

  const handleError = (e: SyntheticEvent<HTMLImageElement, Event>) => {
    if (index + 1 < chain.length) {
      setIndex((i) => i + 1);
      return;
    }
    onError?.(e);
    setIndex((i) => i + 1);
  };

  return (
    <img
      src={toAbsoluteUrl(chain[index])}
      alt={alt}
      {...rest}
      onError={handleError}
    />
  );
}
