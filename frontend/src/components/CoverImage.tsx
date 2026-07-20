import type { ImgHTMLAttributes, ReactNode } from "react";
import { toAbsoluteUrl } from "../api/instanceUrl";

/**
 * Book cover &lt;img&gt; that resolves root-relative /api/… URLs against the
 * Library server on Capacitor (WebView origin is https://localhost).
 */
type Props = Omit<ImgHTMLAttributes<HTMLImageElement>, "src"> & {
  src?: string | null;
  fallback?: ReactNode;
};

export default function CoverImage({ src, fallback = null, alt = "", ...rest }: Props) {
  if (!src) return <>{fallback}</>;
  return <img src={toAbsoluteUrl(src)} alt={alt} {...rest} />;
}
