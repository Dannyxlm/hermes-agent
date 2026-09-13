import { describe, expect, it } from "vitest";
import { pluginActivationLabel } from "./plugin-activation-label";

describe("plugin activation labels", () => {
  it("does not claim an automatically activated plugin is inactive", () => {
    expect(pluginActivationLabel("inactive")).toBe("Default activation");
    expect(pluginActivationLabel("enabled")).toBe("Explicitly enabled");
    expect(pluginActivationLabel("disabled")).toBe("Explicitly disabled");
    expect(pluginActivationLabel("unknown")).toBe("unknown");
  });
});
