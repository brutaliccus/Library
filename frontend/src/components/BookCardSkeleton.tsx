export default function BookCardSkeleton() {
  return (
    <div className="flex flex-col bg-gray-800/50 rounded-lg overflow-hidden border border-gray-800 animate-pulse">
      <div className="aspect-[2/3] bg-gray-700/50" />
      <div className="p-1.5 flex flex-col gap-1">
        <div className="h-2.5 bg-gray-700/50 rounded w-full" />
        <div className="h-2 bg-gray-700/30 rounded w-2/3" />
      </div>
    </div>
  );
}
