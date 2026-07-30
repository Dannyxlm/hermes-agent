import { describe, expect, it } from "vitest";

import type { ManagedUpdateSource } from "./api";
import { presentManagedUpdate } from "./managed-update-status";

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
