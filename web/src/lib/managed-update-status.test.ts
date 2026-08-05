import { describe, expect, it } from "vitest";

import type { ManagedUpdateSource } from "./api";
import {
  presentManagedRefresh,
  presentManagedUpdate,
} from "./managed-update-status";

function managedSource(
  overrides: Partial<ManagedUpdateSource> = {},
): ManagedUpdateSource {
  return {
    schema_version: "hermes-update-status.v2",
    count_basis: "running_source",
    availability: "ready",
    stale: false,
    status_error: null,
    commits_behind: 2_491,
    local_patch_count: 3,
    can_build_candidate: false,
    candidate_request_available: false,
    refresh_request_available: false,
    refresh_request: null,
    ...overrides,
  };
}

describe("presentManagedUpdate", () => {
  it("keeps the official upstream count visible for a read-only managed runtime", () => {
    expect(presentManagedUpdate(managedSource())).toEqual({
      badge: "2,491 upstream behind",
      blocked: false,
      headline: "2,491 official upstream commits behind",
      tone: "warning",
    });
  });

  it("reports the exact official distance from immutable release provenance", () => {
    expect(
      presentManagedUpdate(
        managedSource({
          count_basis: "recorded_official_base",
          commits_behind: 0,
          running_source_is_ancestor_of_upstream: false,
          overlay_count: 8,
        }),
      ),
    ).toEqual({
      badge: "upstream current",
      blocked: false,
      headline:
        "This immutable release is based on the current official upstream head.",
      tone: "success",
    });
  });

  it("does not invent a behind count for a non-ancestral managed fork", () => {
    expect(
      presentManagedUpdate(
        managedSource({
          count_basis: "unavailable_non_ancestral",
          commits_behind: undefined,
          running_source_is_ancestor_of_upstream: false,
          candidate_status: "ready",
          candidate_target_revision: "d".repeat(40),
          candidate_target_is_ancestor_of_upstream: true,
          candidate_target_commits_behind: 17,
        }),
      ),
    ).toEqual({
      badge: "managed fork",
      blocked: false,
      headline:
        'This fork has verified upstream provenance; a direct "commits behind" count does not apply.',
      tone: "success",
    });
  });

  it("labels a stale count as last known instead of presenting it as fresh", () => {
    expect(
      presentManagedUpdate(
        managedSource({ availability: "stale", stale: true }),
      ),
    ).toMatchObject({
      badge: "status stale",
      headline: "2,491 official upstream commits behind (last known)",
      tone: "warning",
    });
  });

  it("separately flags source-monitor blockers", () => {
    expect(
      presentManagedUpdate(
        managedSource({ blockers: ["running source provenance is unresolved"] }),
      ).blocked,
    ).toBe(true);
  });

  it("fails soft when a ready receipt omits its upstream count", () => {
    expect(
      presentManagedUpdate(managedSource({ commits_behind: undefined })),
    ).toMatchObject({
      badge: "count unavailable",
      tone: "secondary",
    });
  });
});

describe("presentManagedRefresh", () => {
  it("does not translate a rejected refresh into a false latest result", () => {
    expect(
      presentManagedRefresh(
        managedSource({
          refresh_request_available: true,
          refresh_request: {
            requested: false,
            error: "refresh_request_unavailable",
          },
        }),
      ),
    ).toEqual({
      message: "refresh_request_unavailable",
      tone: "error",
    });
  });

  it("reports an accepted request without claiming the old receipt is current", () => {
    expect(
      presentManagedRefresh(
        managedSource({
          refresh_request_available: true,
          refresh_request: { requested: true, error: null },
        }),
      ),
    ).toEqual({
      message:
        "Source-monitor refresh requested. The status will update after the monitor finishes.",
      tone: "success",
    });
  });
});
