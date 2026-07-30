declare module "foliate-js/view.js" {
  export class View extends HTMLElement {
    book: {
      toc?: Array<{
        label?: string;
        href?: string;
        subitems?: unknown[];
      }>;
      metadata?: { title?: string | Record<string, string> };
      dir?: string;
      sections?: unknown[];
      transformTarget?: EventTarget;
    };
    renderer: {
      setAttribute(name: string, value: string): void;
      setStyles?: (styles: string | string[]) => void;
      next: (distance?: number) => Promise<void>;
      prev: (distance?: number) => Promise<void>;
      destroy?: () => void;
    };
    lastLocation?: {
      cfi?: string;
      fraction?: number;
      section?: number;
      location?: { current?: number; next?: number; total?: number };
      tocItem?: { label?: string; href?: string };
    };
    open(book: File | Blob | string): Promise<void>;
    close(): void;
    init(opts: { lastLocation?: string; showTextStart?: boolean }): Promise<void>;
    goTo(target: string | number): Promise<unknown>;
    goToFraction(frac: number): Promise<void>;
    prev(distance?: number): Promise<void>;
    next(distance?: number): Promise<void>;
    goLeft(): Promise<void> | void;
    goRight(): Promise<void> | void;
  }
}
