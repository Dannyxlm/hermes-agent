/** The hub reports explicit config membership, not runtime connection health. */
export function pluginActivationLabel(status: string): string {
  if (status === "enabled") return "Explicitly enabled";
  if (status === "disabled") return "Explicitly disabled";
  if (status === "inactive") return "Default activation";
  return status;
}
