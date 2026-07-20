import { useState, useEffect } from "react";
import { toAbsoluteUrl } from "../api/instanceUrl";

/** Fetches image with auth token so img-like sources work for protected endpoints. */
interface Props {
  src: string;
  alt: string;
  className?: string;
  fallback?: React.ReactNode;
}

export default function AuthImage({ src, alt, className, fallback }: Props) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!src) {
      setError(true);
      return;
    }
    let revoked = false;
    const token = localStorage.getItem("access_token");
    const url = toAbsoluteUrl(src);
    fetch(url, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      credentials: "include",
    })
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load");
        return res.blob();
      })
      .then((blob) => {
        if (revoked) return;
        setObjectUrl(URL.createObjectURL(blob));
        setError(false);
      })
      .catch(() => {
        if (!revoked) setError(true);
      });
    return () => {
      revoked = true;
    };
  }, [src]);

  useEffect(() => {
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [objectUrl]);

  if (error || !objectUrl) {
    return <>{fallback ?? null}</>;
  }
  return <img src={objectUrl} alt={alt} className={className} loading="lazy" />;
}
