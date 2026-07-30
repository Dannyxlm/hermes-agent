import type { ManagedUpdateSource } from "./api";

export type ManagedUpdateTone =
  | "success"
  | "warning"
  | "destructive"
  | "secondary";

export interface ManagedUpdatePresentation {
  badge: string;
  blocked: boolean;
  headline: string;
  tone: ManagedUpdateTone;
}

function commitsLabel(count: number): string {
  return `${count.toLocaleString("en-US")} official upstream commit${
    count === 1 ? "" : "s"
  } behind`;
}

export function presentManagedUpdate(
  source: ManagedUpdateSource,
): ManagedUpdatePresentation {
  const behind = source.commits_behind;
  const blocked =
    source.candidate_status === "blocked" || Boolean(source.blockers?.length);

  if (source.availability === "ready") {
    if (behind === 0) {
      return {
        badge: "upstream current",
        blocked,
        headline: "This immutable release matches official upstream.",
        tone: "success",
      };
    }
    if (typeof behind === "number") {
      return {
        badge: `${behind.toLocaleString("en-US")} upstream behind`,
        blocked,
        headline: commitsLabel(behind),
        tone: "warning",
      };
    }
    return {
      badge: "count unavailable",
      blocked,
      headline: "The official upstream count was not reported.",
      tone: "secondary",
    };
  }

  if (source.availability === "stale") {
    return {
      badge: "status stale",
      blocked,
      headline:
        typeof behind === "number"
          ? `${commitsLabel(behind)} (last known)`
          : "The official upstream count is stale.",
      tone: "warning",
    };
  }

  const unavailable: Record<
    Exclude<ManagedUpdateSource["availability"], "ready" | "stale">,
    { badge: string; headline: string; tone: ManagedUpdateTone }
  > = {
    missing: {
      badge: "status unavailable",
      headline: "No immutable source-monitor receipt is available.",
      tone: "secondary",
    },
    invalid: {
      badge: "status invalid",
      headline: "The immutable source-monitor receipt is invalid.",
      tone: "destructive",
    },
    unreadable: {
      badge: "status unreadable",
      headline: "The immutable source-monitor receipt could not be read.",
      tone: "destructive",
    },
  };
  return { ...unavailable[source.availability], blocked };
}
