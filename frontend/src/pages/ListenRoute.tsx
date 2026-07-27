import { useEffect } from "react";
import { Navigate } from "react-router-dom";
import { usePlayer } from "../contexts/PlayerContext";

/** Deep link `/listen` — expand the player when something is playing, else go to My Library. */
export default function ListenRoute() {
  const { nowPlaying, setExpanded } = usePlayer();

  useEffect(() => {
    if (nowPlaying) setExpanded(true);
  }, [nowPlaying, setExpanded]);

  if (nowPlaying) return null;
  return <Navigate to="/my-library" replace />;
}
