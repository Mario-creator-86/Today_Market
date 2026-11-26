/*
 * WHY IS X MOVING: the driver rows of a decision. Pure; its only runtime
 * import is relative (vitest does not resolve "@/"), so lib/market-data.test.ts
 * can collect it.
 */
import type { Decision, DriverContributions } from "@/lib/api";
import { humanize, isNum } from "../../lib/format";

const COMPONENT_LABELS: Record<string, string> = {
  event: "Events",
  macro: "Macro",
  reaction: "Market reaction",
  technical: "Technical",
  positioning: "Positioning",
  cross_asset: "Cross-asset",
  regime: "Regime",
};

export function componentLabel(name: string): string {
  return COMPONENT_LABELS[name] ?? humanize(name);
}

/**
 * Score components each driver row is built from (backend
 * scoring.driver_contributions): Technical and Positioning from their own
 * component, Other from market reaction, cross-asset, regime and unmapped
 * event paths, and every causal driver (Rates, USD, Geopolitics/Energy, …)
 * from event and macro paths.
 */
const DRIVER_SOURCES: Record<string, string[]> = {
  Technical: ["technical"],
  Positioning: ["positioning"],
  Other: ["event", "macro", "reaction", "cross_asset", "regime"],
};
const CAUSAL_SOURCES = ["event", "macro"];

/** A driver none of whose source components was measured: missing, not a measured zero. */
function driverMissing(driver: string, unmeasured: Set<string>): boolean {
  const sources = DRIVER_SOURCES[driver] ?? CAUSAL_SOURCES;
  return unmeasured.size > 0 && sources.every((s) => unmeasured.has(s));
}

export type DriverRow = { name: string; value: number | null; basis?: string };
export type DriverModel = {
  rows: DriverRow[];
  net: number | null;
  /** Components that were not measured (never shown as a measured zero). */
  unmeasured: string[];
  basis: string;
};

/**
 * Driver rows from the richest shape the API provides: `drivers` (the
 * published decision; older payloads and fixtures say `driver_contributions`),
 * else the score components.
 */
export function driverModel(
  d: Decision & { drivers?: DriverContributions | null },
): DriverModel | null {
  const dc = d.drivers ?? d.driver_contributions;
  if (dc && Array.isArray(dc.rows) && dc.rows.length) {
    const unmeasured = dc.unmeasured ?? d.components_missing ?? [];
    const missing = new Set(unmeasured);
    return {
      rows: dc.rows.map((r) => ({
        name: r.driver,
        value:
          r.status === "missing" ||
          !isNum(r.value) ||
          (r.value === 0 && driverMissing(r.driver, missing))
            ? null
            : r.value,
      })),
      net: isNum(dc.net) ? dc.net : isNum(d.score) ? d.score : null,
      unmeasured: unmeasured.map(componentLabel),
      basis: "Signed weighted contribution by driver",
    };
  }
  const detail = d.component_detail;
  if (detail && Object.keys(detail).length) {
    const rows = Object.entries(detail).map(([name, c]) => {
      const measured = c?.status === "measured" && isNum(c.value);
      const weighted = d.components?.[name];
      return {
        name: componentLabel(name),
        value: measured
          ? isNum(weighted)
            ? weighted
            : (c.value as number)
          : null,
        basis: c?.basis,
      };
    });
    return {
      rows,
      net: isNum(d.score) ? d.score : null,
      unmeasured: [],
      basis: "Signed weighted contribution by score component",
    };
  }
  const legacy = Object.entries(d.components ?? {});
  if (!legacy.length) return null;
  const missing = new Set(d.components_missing ?? []);
  return {
    rows: legacy.map(([name, value]) => ({
      name: componentLabel(name),
      value: missing.has(name) || !isNum(value) ? null : value,
    })),
    net: isNum(d.score) ? d.score : null,
    unmeasured: [],
    basis: "Signed contribution by score component",
  };
}
