import { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { ChevronDown } from "lucide-react";

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
  onSelect: (slugs: string[]) => void;
}

interface DropdownPos {
  top: number;
  left: number;
}

export default function GenreBar({ genres, activeSlugs = [], onSelect }: Props) {
  const [openDropdown, setOpenDropdown] = useState<string | null>(null);
  const [dropdownPos, setDropdownPos] = useState<DropdownPos>({ top: 0, left: 0 });
  const barRef = useRef<HTMLDivElement>(null);
  const btnRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const closeDropdown = useCallback(() => setOpenDropdown(null), []);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        const portal = document.getElementById("genre-dropdown-portal");
        if (portal && portal.contains(e.target as Node)) return;
        closeDropdown();
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [closeDropdown]);

  useEffect(() => {
    if (!openDropdown) return;
    const handleScroll = () => closeDropdown();
    window.addEventListener("scroll", handleScroll, true);
    return () => window.removeEventListener("scroll", handleScroll, true);
  }, [openDropdown, closeDropdown]);

  const isParentActive = (genre: Genre) =>
    activeSlugs.includes(genre.slug) ||
    genre.children.some((c) => activeSlugs.includes(c.slug));

  const handleParentClick = (genre: Genre) => {
    if (activeSlugs.includes(genre.slug)) {
      onSelect(activeSlugs.filter((s) => s !== genre.slug && !genre.children.some((c) => c.slug === s)));
    } else {
      const without = activeSlugs.filter((s) => !genre.children.some((c) => c.slug === s));
      onSelect([...without, genre.slug]);
    }
    closeDropdown();
  };

  const handleChildClick = (parent: Genre, child: SubGenre) => {
    const parentRemoved = activeSlugs.filter((s) => s !== parent.slug);
    if (parentRemoved.includes(child.slug)) {
      const next = parentRemoved.filter((s) => s !== child.slug);
      onSelect(next);
    } else {
      onSelect([...parentRemoved, child.slug]);
    }
    closeDropdown();
  };

  const toggleDropdown = (slug: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (openDropdown === slug) {
      closeDropdown();
      return;
    }
    const btn = btnRefs.current[slug];
    if (btn) {
      const rect = btn.getBoundingClientRect();
      setDropdownPos({
        top: rect.bottom + 6,
        left: rect.left,
      });
    }
    setOpenDropdown(slug);
  };

  const specials: { slug: string; name: string }[] = [
    { slug: "all", name: "All" },
    { slug: "popular", name: "Popular" },
    { slug: "new", name: "New" },
  ];

  const openGenre = genres.find((g) => g.slug === openDropdown);

  return (
    <div ref={barRef}>
      <div className="flex items-center gap-1.5 overflow-x-auto pb-2 scrollbar-hide">
        {specials.map((s) => {
          const isActive = activeSlugs.includes(s.slug);
          return (
            <button
              key={s.slug}
              onClick={() => onSelect(isActive ? [] : [s.slug])}
              className={`shrink-0 px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                isActive
                  ? "bg-brand-600 text-white shadow-md shadow-brand-600/20"
                  : "bg-gray-800/80 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
              }`}
            >
              {s.name}
            </button>
          );
        })}

        <div className="w-px h-6 bg-gray-700/60 shrink-0 mx-1" />

        {genres.map((genre) => {
          const active = isParentActive(genre);
          const hasChildren = genre.children.length > 0;
          const isOpen = openDropdown === genre.slug;
          const activeChild = genre.children.find((c) => activeSlugs.includes(c.slug));

          return (
            <div key={genre.slug} className="shrink-0">
              <div className="flex items-center">
                <button
                  onClick={() => handleParentClick(genre)}
                  className={`px-3.5 py-2 text-sm font-medium transition-all ${
                    hasChildren ? "rounded-l-lg" : "rounded-lg"
                  } ${
                    active
                      ? "bg-brand-600 text-white shadow-md shadow-brand-600/20"
                      : "bg-gray-800/80 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
                  }`}
                >
                  {activeChild ? activeChild.name : genre.name}
                </button>
                {hasChildren && (
                  <button
                    ref={(el) => { btnRefs.current[genre.slug] = el; }}
                    onClick={(e) => toggleDropdown(genre.slug, e)}
                    className={`px-1.5 py-2 rounded-r-lg border-l transition-all ${
                      active
                        ? "bg-brand-600 text-white border-brand-500 hover:bg-brand-500"
                        : "bg-gray-800/80 text-gray-500 border-gray-700/50 hover:bg-gray-700 hover:text-gray-300"
                    }`}
                    aria-label={`${genre.name} sub-genres`}
                  >
                    <ChevronDown
                      size={14}
                      className={`transition-transform ${isOpen ? "rotate-180" : ""}`}
                    />
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {openGenre &&
        createPortal(
          <div
            id="genre-dropdown-portal"
            className="fixed z-[9999] min-w-[200px] bg-gray-800 border border-gray-700 rounded-xl shadow-2xl shadow-black/50 overflow-hidden"
            style={{ top: dropdownPos.top, left: dropdownPos.left }}
          >
            <div className="py-1.5 max-h-[60vh] overflow-y-auto">
              <button
                onClick={() => handleParentClick(openGenre)}
                className={`w-full text-left px-4 py-2 text-sm transition-colors ${
                  activeSlugs.includes(openGenre.slug)
                    ? "bg-brand-600/20 text-brand-300 font-medium"
                    : "text-gray-300 hover:bg-gray-700/60 hover:text-white"
                }`}
              >
                All {openGenre.name}
              </button>
              <div className="mx-3 my-1 border-t border-gray-700/50" />
              {openGenre.children.map((child) => {
                const childActive = activeSlugs.includes(child.slug);
                return (
                  <button
                    key={child.slug}
                    onClick={() => handleChildClick(openGenre, child)}
                    className={`w-full text-left px-4 py-2 text-sm transition-colors ${
                      childActive
                        ? "bg-brand-600/20 text-brand-300 font-medium"
                        : "text-gray-300 hover:bg-gray-700/60 hover:text-white"
                    }`}
                  >
                    {child.name}
                  </button>
                );
              })}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
