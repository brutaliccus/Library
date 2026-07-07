import { Star } from "lucide-react";

interface Props {
  rating: number;
  count?: number;
  size?: number;
}

export default function StarRating({ rating, count, size = 14 }: Props) {
  if (!rating) return null;

  const stars = [];
  for (let i = 1; i <= 5; i++) {
    if (rating >= i) {
      stars.push(<Star key={i} size={size} className="fill-amber-400 text-amber-400" />);
    } else if (rating >= i - 0.5) {
      stars.push(
        <span key={i} className="relative inline-block" style={{ width: size, height: size }}>
          <Star size={size} className="text-gray-600 absolute inset-0" />
          <span className="absolute inset-0 overflow-hidden" style={{ width: "50%" }}>
            <Star size={size} className="fill-amber-400 text-amber-400" />
          </span>
        </span>
      );
    } else {
      stars.push(<Star key={i} size={size} className="text-gray-600" />);
    }
  }

  return (
    <div className="flex items-center gap-1">
      <div className="flex items-center gap-0.5">{stars}</div>
      {count !== undefined && count > 0 && (
        <span className="text-xs text-gray-400 ml-1">({count.toLocaleString()})</span>
      )}
    </div>
  );
}
