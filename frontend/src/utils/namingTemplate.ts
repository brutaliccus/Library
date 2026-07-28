/** LibraForge Folder Forge naming-template helpers (parse / build / preview). */

export const DEFAULT_NAMING_TEMPLATE =
  "{author}/{series} [{edition}]/{title}/{filename}";

export const NAMING_TOKENS = [
  { id: "author", label: "Author", example: "Brandon Sanderson" },
  { id: "series", label: "Series", example: "Mistborn" },
  { id: "edition", label: "Edition", example: "GraphicAudio" },
  { id: "title", label: "Title", example: "The Final Empire" },
  { id: "subtitle", label: "Subtitle", example: "A Reckoners Novel" },
  { id: "order", label: "Order", example: "Book 1" },
  { id: "number", label: "Number", example: "1" },
  { id: "narrator", label: "Narrator", example: "Michael Kramer" },
  { id: "publisher", label: "Publisher", example: "Tor Books" },
  { id: "year", label: "Year", example: "2006" },
  { id: "asin", label: "ASIN", example: "B002UZZDDU" },
  { id: "filename", label: "Filename", example: "The Final Empire" },
  { id: "original", label: "Original filename", example: "messy_source_name" },
] as const;

export type NamingTokenId = (typeof NAMING_TOKENS)[number]["id"];

const TOKEN_IDS = new Set<string>(NAMING_TOKENS.map((t) => t.id));

export const SEGMENT_PRESETS: Array<{
  id: string;
  label: string;
  template: string;
}> = [
  { id: "author", label: "Author", template: "{author}" },
  { id: "series", label: "Series", template: "{series}" },
  { id: "series-edition", label: "Series [Edition]", template: "{series} [{edition}]" },
  { id: "title", label: "Title", template: "{title}" },
  { id: "order-title", label: "Order - Title", template: "{order} - {title}" },
  { id: "narrator", label: "Narrator", template: "{narrator}" },
  { id: "year", label: "Year", template: "{year}" },
  { id: "asin", label: "ASIN", template: "{asin}" },
  { id: "filename", label: "Filename", template: "{filename}" },
  { id: "original", label: "Original filename", template: "{original}" },
];

export type SegmentPart =
  | { kind: "token"; token: string }
  | { kind: "text"; text: string };

export type NamingSegment = { parts: SegmentPart[] };

const TOKEN_RE = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

export function tokenLabel(token: string): string {
  const found = NAMING_TOKENS.find((t) => t.id === token);
  return found?.label ?? token;
}

export function parseSegment(segment: string): NamingSegment | null {
  if (!segment.length) return { parts: [] };
  const parts: SegmentPart[] = [];
  const re = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(segment)) !== null) {
    if (m.index > last) {
      parts.push({ kind: "text", text: segment.slice(last, m.index) });
    }
    const name = m[1];
    if (!TOKEN_IDS.has(name)) {
      return null;
    }
    parts.push({ kind: "token", token: name });
    last = m.index + m[0].length;
  }
  if (last < segment.length) {
    parts.push({ kind: "text", text: segment.slice(last) });
  }
  // Reject leftover braces that weren't valid tokens.
  const stripped = segment.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, "");
  if (stripped.includes("{") || stripped.includes("}")) {
    return null;
  }
  return { parts };
}

export type ParseResult =
  | { ok: true; segments: NamingSegment[] }
  | { ok: false; reason: string };

export function parseNamingTemplate(template: string): ParseResult {
  const raw = (template || "").trim();
  if (!raw) {
    return parseNamingTemplate(DEFAULT_NAMING_TEMPLATE);
  }
  if (!raw.includes("/")) {
    return { ok: false, reason: "Template needs at least one '/' folder separator." };
  }
  const segments: NamingSegment[] = [];
  for (const seg of raw.split("/")) {
    const parsed = parseSegment(seg);
    if (!parsed) {
      return { ok: false, reason: "Could not parse a path segment into known tokens." };
    }
    segments.push(parsed);
  }
  if (!segments.length) {
    return { ok: false, reason: "Empty template." };
  }
  return { ok: true, segments };
}

export function serializeSegment(segment: NamingSegment): string {
  return segment.parts
    .map((p) => (p.kind === "token" ? `{${p.token}}` : p.text))
    .join("");
}

export function serializeNamingTemplate(segments: NamingSegment[]): string {
  return segments.map(serializeSegment).join("/");
}

export function segmentFromPreset(template: string): NamingSegment | null {
  return parseSegment(template);
}

export function humanSegmentLabel(segment: NamingSegment): string {
  if (!segment.parts.length) return "(empty)";
  return segment.parts
    .map((p) => (p.kind === "token" ? tokenLabel(p.token) : p.text))
    .join("")
    .trim() || "(empty)";
}

const SAMPLE: Record<string, string> = Object.fromEntries(
  NAMING_TOKENS.map((t) => [t.id, t.example]),
);

/** Soft preview: empty tokens still show placeholder text for illustration. */
export function previewExamplePath(
  template: string,
  samples: Record<string, string> = SAMPLE,
): string {
  const rendered = template.replace(TOKEN_RE, (_m, name: string) => {
    const v = samples[name];
    return v != null && v !== "" ? v : `{${name}}`;
  });
  // Collapse empty brackets for illustration when edition present in samples.
  return rendered.replace(/\s*\[\s*\]/g, "").replace(/\/{2,}/g, "/");
}

export function defaultSegments(): NamingSegment[] {
  const parsed = parseNamingTemplate(DEFAULT_NAMING_TEMPLATE);
  return parsed.ok ? parsed.segments : [];
}
