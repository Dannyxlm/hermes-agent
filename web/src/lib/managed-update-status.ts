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

export interface ManagedRefreshPresentation {
  message: string;
  tone: "error" | "success";
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
    if (
      source.count_basis === "unavailable_non_ancestral" &&
      source.running_source_is_ancestor_of_upstream === false
    ) {
      return {
        badge: "managed fork",
        blocked,
        headline:
          'This fork has verified upstream provenance; a direct "commits behind" count does not apply.',
        tone: "success",
      };
    }
    if (behind === 0) {
      return {
        badge: "upstream current",
        blocked,
        headline:
          source.count_basis === "recorded_official_base"
            ? "This immutable release is based on the current official upstream head."
            : "This immutable release matches official upstream.",
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
    if (source.count_basis === "unavailable_non_ancestral") {
      return {
        badge: "status stale",
        blocked,
        headline:
          "Managed-fork provenance is stale; no direct upstream count is being claimed.",
        tone: "warning",
      };
    }
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

export function presentManagedRefresh(
  source: ManagedUpdateSource,
): ManagedRefreshPresentation {
  if (!source.refresh_request_available) {
    return {
      message:
        "This runtime cannot request a source-monitor refresh from the dashboard.",
      tone: "error",
    };
  }

  if (!source.refresh_request?.requested) {
    return {
      message:
        source.refresh_request?.error ??
        "The source-monitor refresh request was not accepted.",
      tone: "error",
    };
  }

  return {
    message:
      "Source-monitor refresh requested. The status will update after the monitor finishes.",
    tone: "success",
  };
}
