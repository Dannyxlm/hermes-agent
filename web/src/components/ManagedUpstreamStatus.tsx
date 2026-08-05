import { GitBranch, RotateCw } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

import type { ManagedUpdateSource } from "@/lib/api";
import { presentManagedUpdate } from "@/lib/managed-update-status";

interface ManagedUpstreamStatusProps {
  checking: boolean;
  onReload: () => void;
  source?: ManagedUpdateSource;
}

function formatAge(seconds: number): string {
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  if (days > 0) return `${days}d ${hours}h ${minutes}m`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function shortRevision(revision: string | null | undefined): string {
  return revision ? revision.slice(0, 12) : "not reported";
}

function stateLabel(state: string | undefined): string {
  return state ? state.replaceAll("_", " ") : "not reported";
}

export function ManagedUpstreamStatus({
  checking,
  onReload,
  source,
}: ManagedUpstreamStatusProps) {
  const status = source ? presentManagedUpdate(source) : null;

  return (
    <div className="mt-4 border-t border-border pt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <GitBranch className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium">Official upstream</span>
            {status && <Badge tone={status.tone}>{status.badge}</Badge>}
            {status?.blocked && (
              <Badge tone="warning">update train blocked</Badge>
            )}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">
            {status?.headline ??
              "This managed runtime does not expose an upstream receipt yet."}
          </p>
        </div>
        <Button
          size="sm"
          ghost
          disabled={checking || source?.refresh_request_available !== true}
          prefix={
            checking ? (
              <Spinner className="h-3.5 w-3.5" />
            ) : (
              <RotateCw className="h-3.5 w-3.5" />
            )
          }
          onClick={onReload}
        >
          {source?.refresh_request_available === true
            ? "Request refresh"
            : "Refresh unavailable"}
        </Button>
      </div>

      {source && (
        <>
          <div className="mt-4 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Running release
              </div>
              <div className="break-all font-mono text-xs">
                {source.running_release ?? "not reported"}
              </div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Running source
              </div>
              <div className="font-mono text-xs">
                {shortRevision(source.running_source)}
              </div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Upstream base
              </div>
              <div className="font-mono text-xs">
                {shortRevision(source.running_upstream_base)}
              </div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Official head
              </div>
              <div className="font-mono text-xs">
                {source.tracked_upstream ?? "not reported"}
                {source.upstream_head
                  ? ` @ ${shortRevision(source.upstream_head)}`
                  : ""}
              </div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                CloudSeed overlays
              </div>
              <div>
                {typeof source.overlay_count === "number"
                  ? `${source.overlay_count.toLocaleString("en-US")} maintained feature${
                      source.overlay_count === 1 ? "" : "s"
                    }`
                  : "not reported"}
              </div>
              {source.overlay_ids && source.overlay_ids.length > 0 && (
                <div className="mt-1 text-xs text-muted-foreground">
                  {source.overlay_ids.join(", ")}
                </div>
              )}
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Carried Git commits
              </div>
              <div>
                {typeof source.carried_commit_count === "number"
                  ? source.carried_commit_count.toLocaleString("en-US")
                  : "diagnostic unavailable"}
              </div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Monitor freshness
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <span>
                  {typeof source.age_seconds === "number"
                    ? `${formatAge(source.age_seconds)} ago`
                    : "not reported"}
                </span>
                {source.stale && <Badge tone="warning">stale</Badge>}
              </div>
              {source.last_fetched_at && (
                <time
                  className="block break-all font-mono text-xs text-muted-foreground"
                  dateTime={source.last_fetched_at}
                >
                  {source.last_fetched_at}
                </time>
              )}
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Candidate state
              </div>
              <div>{stateLabel(source.candidate_status)}</div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-muted-foreground">
                Release identity
              </div>
              <div>{source.hermes_version ? `Hermes ${source.hermes_version}` : "version not reported"}</div>
              <div className="break-all font-mono text-xs text-muted-foreground">
                {source.release_id ?? "release ID not reported"}
              </div>
            </div>
          </div>

          {source.blockers && source.blockers.length > 0 && (
            <div className="mt-4 border border-border bg-background/40 p-3">
              <div className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                Update train blockers
              </div>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
                {source.blockers.map((blocker, index) => (
                  <li key={`${index}:${blocker}`}>{blocker}</li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      <p className="mt-4 text-xs text-muted-foreground">
        Read-only here: managed Hermes runtimes move through reviewed immutable
        releases, never an in-place dashboard update.
      </p>
    </div>
  );
}
