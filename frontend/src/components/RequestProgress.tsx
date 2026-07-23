import RequestPipelineSteps from "./RequestPipelineSteps";

export interface RequestProgressData {
  status: string;
  detail?: string | null;
  progress_percent?: number | null;
  progress_bytes?: number | null;
  progress_total_bytes?: number | null;
  progress_speed_bps?: number | null;
}

/** Renders multi-step pipeline progress (download → metadata → m4b → folder → finalize). */
export default function RequestProgress(props: RequestProgressData) {
  return <RequestPipelineSteps {...props} />;
}
