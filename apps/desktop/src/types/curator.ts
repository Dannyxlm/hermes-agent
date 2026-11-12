/** `GET /api/curator` — background skill-curator status. */
export interface CuratorStatusResponse {
  enabled: boolean
  paused: boolean
  interval_hours: number | null
  last_run_at: string | null
  min_idle_hours: number | null
  stale_after_days: number | null
  archive_after_days: number | null
}