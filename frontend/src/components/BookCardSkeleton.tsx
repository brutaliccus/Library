export default function BookCardSkeleton() {
  return (
    <div className="flex flex-col animate-pulse">
      <div className="aspect-[2/3] bg-gray-700/50 rounded-lg border border-gray-800" />
      <div className="pt-1.5 px-0.5 pb-0.5 flex flex-col gap-1">
        <div className="h-2.5 bg-gray-700/50 rounded w-full" />
        <div className="h-2 bg-gray-700/30 rounded w-2/3" />
        <div className="h-2 bg-gray-700/20 rounded w-1/2" />
      </div>
    </div>
  );
}
