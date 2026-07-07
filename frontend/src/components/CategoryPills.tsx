interface Category {
  slug: string;
  name: string;
}

interface Props {
  categories: Category[];
  activeSlugs?: string[];
  onToggle?: (slugs: string[]) => void;
}

export default function CategoryPills({ categories, activeSlugs = [], onToggle }: Props) {
  const handleClick = (slug: string) => {
    if (!onToggle) return;

    const specials = ["all", "popular", "new"];
    if (specials.includes(slug)) {
      onToggle([slug]);
      return;
    }

    const filtered = activeSlugs.filter((s) => !specials.includes(s));
    const isActive = filtered.includes(slug);
    const next = isActive
      ? filtered.filter((s) => s !== slug)
      : [...filtered, slug];
    onToggle(next.length === 0 ? [] : next);
  };

  return (
    <div className="flex flex-wrap gap-2">
      {categories.map((cat) => {
        const isActive = activeSlugs.includes(cat.slug);
        return (
          <button
            key={cat.slug}
            onClick={() => handleClick(cat.slug)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium transition-colors ${
              isActive
                ? "bg-brand-600 text-white"
                : "bg-gray-800 text-gray-300 hover:bg-gray-700 hover:text-white"
            }`}
          >
            {cat.name}
          </button>
        );
      })}
    </div>
  );
}
