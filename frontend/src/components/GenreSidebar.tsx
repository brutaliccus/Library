import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight, X } from "lucide-react";

export interface SubGenre {
  slug: string;
  name: string;
}

export interface Genre {
  slug: string;
  name: string;
  icon?: string;
  children: SubGenre[];
}

interface Props {
  genres: Genre[];
  activeSlugs?: string[];
  /** filter = multi-select for search; navigate = browse pages (home). */
  mode?: "filter" | "navigate";
  onSelect?: (slugs: string[]) => void;
  mobileOpen?: boolean;
  onMobileClose?: () => void;
}

export default function GenreSidebar({
  genres,
  activeSlugs = [],
  mode = "filter",
  onSelect,
  mobileOpen = false,
  onMobileClose,
}: Props) {
  const navigate = useNavigate();
  const [expandedSlugs, setExpandedSlugs] = useState<Set<string>>(() => {
    const initial = new Set<string>();
    for (const g of genres) {
      if (
        activeSlugs.includes(g.slug) ||
        g.children.some((c) => activeSlugs.includes(c.slug))
      ) {
        initial.add(g.slug);
      }
    }
    return initial;
  });

  useEffect(() => {
    if (mobileOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => { document.body.style.overflow = ""; };
  }, [mobileOpen]);

  const toggleExpand = (slug: string) => {
    setExpandedSlugs((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  };

  const isParentActive = (genre: Genre) =>
    activeSlugs.includes(genre.slug) ||
    genre.children.some((c) => activeSlugs.includes(c.slug));

  const closeMobile = () => onMobileClose?.();

  const goParent = (genre: Genre) => {
    closeMobile();
    if (mode === "navigate") {
      if (genre.children.length > 0) {
        navigate(`/genre/${encodeURIComponent(genre.slug)}`);
      } else {
        navigate(`/shelf/${encodeURIComponent(genre.slug)}`);
      }
      return;
    }
    if (!onSelect) return;
    if (activeSlugs.includes(genre.slug)) {
      onSelect(activeSlugs.filter(
        (s) => s !== genre.slug && !genre.children.some((c) => c.slug === s),
      ));
    } else {
      const without = activeSlugs.filter(
        (s) => !genre.children.some((c) => c.slug === s),
      );
      onSelect([...without, genre.slug]);
    }
  };

  const goChild = (parent: Genre, child: SubGenre) => {
    closeMobile();
    if (mode === "navigate") {
      navigate(`/shelf/${encodeURIComponent(child.slug)}`);
      return;
    }
    if (!onSelect) return;
    const parentRemoved = activeSlugs.filter((s) => s !== parent.slug);
    if (parentRemoved.includes(child.slug)) {
      onSelect(parentRemoved.filter((s) => s !== child.slug));
    } else {
      onSelect([...parentRemoved, child.slug]);
    }
  };

  const goSpecial = (slug: string) => {
    closeMobile();
    if (mode === "navigate") {
      navigate(`/shelf/${encodeURIComponent(slug)}`);
      return;
    }
    if (!onSelect) return;
    const active = activeSlugs.includes(slug);
    onSelect(active ? [] : [slug]);
  };

  const specials = [
    { slug: "all", name: "All Available" },
    { slug: "available", name: "Available to Download" },
    { slug: "popular", name: "Popular" },
    { slug: "new", name: "New Releases" },
  ];

  const sidebarContent = (
    <nav className="space-y-0.5">
      <p className="px-3 pt-1 pb-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
        Browse
      </p>

      {specials.map((s) => {
        const active = activeSlugs.includes(s.slug);
        return (
          <button
            key={s.slug}
            onClick={() => goSpecial(s.slug)}
            className={`w-full text-left px-3 py-2 text-sm rounded-lg transition-colors ${
              active
                ? "bg-brand-600/20 text-brand-300 font-medium"
                : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
            }`}
          >
            {s.name}
          </button>
        );
      })}

      <div className="mx-3 my-2 border-t border-gray-800" />

      <p className="px-3 pt-1 pb-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
        Genres
      </p>

      {genres.map((genre) => {
        const active = isParentActive(genre);
        const expanded = expandedSlugs.has(genre.slug);
        const hasChildren = genre.children.length > 0;

        return (
          <div key={genre.slug}>
            <div className="flex items-center">
              <button
                onClick={() => {
                  goParent(genre);
                  if (mode === "filter" && !expanded && hasChildren) toggleExpand(genre.slug);
                }}
                className={`flex-1 text-left px-3 py-2 text-sm rounded-lg transition-colors ${
                  active
                    ? "bg-brand-600/20 text-brand-300 font-medium"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                }`}
              >
                {genre.name}
              </button>
              {hasChildren && (
                <button
                  onClick={() => toggleExpand(genre.slug)}
                  className="p-1.5 mr-1 text-gray-600 hover:text-gray-300 transition-colors"
                  aria-label={expanded ? "Collapse" : "Expand"}
                >
                  <ChevronRight
                    size={14}
                    className={`transition-transform duration-150 ${expanded ? "rotate-90" : ""}`}
                  />
                </button>
              )}
            </div>

            {expanded && hasChildren && (
              <div className="ml-3 pl-3 border-l border-gray-800 space-y-0.5 pb-1">
                {genre.children.map((child) => {
                  const childActive = activeSlugs.includes(child.slug);
                  return (
                    <button
                      key={child.slug}
                      onClick={() => goChild(genre, child)}
                      className={`w-full text-left px-3 py-1.5 text-[13px] rounded-md transition-colors ${
                        childActive
                          ? "bg-brand-600/15 text-brand-300 font-medium"
                          : "text-gray-500 hover:bg-gray-800/60 hover:text-gray-300"
                      }`}
                    >
                      {child.name}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </nav>
  );

  return (
    <>
      {mobileOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <div
            className="absolute inset-0 bg-black/60"
            onClick={closeMobile}
          />
          <div className="absolute left-0 top-0 bottom-0 w-72 bg-gray-900 border-r border-gray-800 overflow-y-auto p-4">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-gray-200">Genres</h3>
              <button
                onClick={closeMobile}
                className="p-1 text-gray-500 hover:text-gray-300 transition-colors"
              >
                <X size={18} />
              </button>
            </div>
            {sidebarContent}
          </div>
        </div>
      )}

      <div className="hidden lg:block w-52 shrink-0 sticky top-[4.5rem] max-h-[calc(100vh-5rem)] overflow-y-auto pr-2 scrollbar-hide">
        {sidebarContent}
      </div>
    </>
  );
}
