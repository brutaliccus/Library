export interface BookSummary {
  id: string;
  title: string;
  subtitle: string;
  authors: string[];
  publisher: string;
  publishedDate: string;
  description: string;
  pageCount: number;
  categories: string[];
  mainCategory: string;
  averageRating: number;
  ratingsCount: number;
  language: string;
  coverUrl: string;
  isbn10: string;
  isbn13: string;
  previewLink: string;
  infoLink: string;
  availability?: {
    available: boolean;
    matchTier?: "exact" | "likely" | "weak";
    torrentCount?: number;
    formats?: string[];
  };
}

export interface BookDetail extends BookSummary {
  coverUrlLarge: string;
  printType: string;
  seriesName?: string;
  seriesBookNumber?: string;
}

export interface SearchResult {
  title: string;
  size: number;
  seeders: number;
  leechers: number;
  indexer: string;
  magnetUrl: string | null;
  downloadUrl: string | null;
  mediaType?: string;
  inLibrary?: string[];
  source?: string;
  aaMd5?: string;
  author?: string;
  fileExtension?: string;
  formatInfo?: string;
  /** Torrent already on Real-Debrid cache (instant download/stream). */
  rdCached?: boolean;
  /** Torrent already on Torbox cache (instant download/stream). */
  torboxCached?: boolean;
  matchScore?: number;
  matchTier?: "exact" | "likely" | "weak";
}
