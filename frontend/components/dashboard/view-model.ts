import type { Alert, Asset, RegimeDriver } from "../../lib/api";
import { number } from "../../lib/api";
import { bucharestDateTime, bucharestDay, bucharestHourMinute } from "../../lib/event-time";
import { isNum } from "../../lib/format";
import { shortDate, signedChange } from "../../lib/market-data";

/*
 * Pure dashboard view models: regime driver inputs, the all-markets index and
 * the alerts list. Relative imports only, so vitest can collect them
 * (lib/dashboard-view-model.test.ts). Every value is the backend's; missing
 * values stay missing.
 */

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

// --- labels -------------------------------------------------------------------------------

const SYMBOL_LABELS: Record<string, string> = { SP500: "S&P 500" };

/** Display name of a symbol on the dashboard: "SP500" → "S&P 500"; others as sent. */
export function symbolLabel(symbol: string): string {
  return SYMBOL_LABELS[symbol] ?? symbol;
}

/** Display name of a regime feature: "SP500" → "S&P 500", "CURVE:US10Y-US2Y" → "US10Y-US2Y curve". */
export function featureLabel(feature: string): string {
  const [kind, name] = feature.includes(":") ? feature.split(":", 2) : ["", feature];
  if (kind === "CURVE") return `${name} curve`;
  if (kind === "CREDIT") return `Credit (${name})`;
  return symbolLabel(name);
}

/** "22 Sep" from a "2026-09-22" close date (a calendar day, never shifted by time zone); null when invalid. */
export function closeDateText(value: unknown): string | null {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  // Noon UTC falls on the same calendar day in Bucharest.
  return shortDate(`${value}T12:00:00Z`);
}

// --- regime driver inputs -----------------------------------------------------------------

export type DriverInputPart = {
  /** "VIX -4.44% (stale close 22 Sep)", "GOLD -1.77% via PAXG", or the backend's own text. */
  text: string;
  /**
   * The close is not the latest one its source's release calendar leads the
   * backend to expect (close_status STALE). It still votes while its market is
   * closed, up to the regime's configured maximum age.
   */
  stale: boolean;
  /** "stale close 22 Sep"; null unless stale. */
  note: string | null;
  /** Tooltip for a stale part: "Not the latest expected close: last close 22 Sep. …"; null unless stale. */
  title: string | null;
};

/** close_status / close_date of an input: its own (regime.drivers[].inputs), else the regime's feature_sources entry. */
function closeOf(input: Record<string, unknown>, sources: unknown): { stale: boolean; day: string | null } {
  const source = isObject(sources) && typeof input.feature === "string" ? sources[input.feature] : null;
  const fallback = isObject(source) ? source : {};
  const status = input.close_status ?? fallback.close_status;
  return {
    stale: String(status ?? "").toUpperCase() === "STALE",
    day: closeDateText(input.close_date ?? fallback.close_date),
  };
}

/** What a STALE flag means (backend sessions.close_status), with the close's own date when known. */
function staleTitle(day: string | null): string {
  return `Not the latest expected close${day ? `: last close ${day}` : ""}. It still counts while its market is closed, up to the configured maximum age.`;
}

/**
 * The inputs that set a driver's arrow, one part each: "US2Y -6 bp",
 * "GOLD -1.77% via PAXG", "VIX -4.44% (stale close 22 Sep)". A close the
 * backend marks STALE (not the latest expected close, still allowed while its
 * market is closed) is flagged with its date, so the chip never presents it
 * as current. The text is always rebuilt from the inputs: the backend's own
 * `input` text carries no stale marker. Without measured inputs that text is
 * used as sent, with the stale inputs named after it; null when the driver
 * reports neither.
 */
export function driverInputParts(item: RegimeDriver, featureSources?: unknown): DriverInputPart[] | null {
  const inputs = (Array.isArray(item.inputs) ? item.inputs : []) as unknown[];
  const parts = inputs.flatMap((i): DriverInputPart[] => {
    if (!isObject(i) || typeof i.feature !== "string" || !isNum(i.value)) return [];
    const change =
      i.field === "change_bp"
        ? signedChange({ change_bp: i.value })
        : i.field === "change_pct"
          ? signedChange({ change_pct: i.value })
          : null;
    const via = typeof i.via === "string" && i.via.trim() ? ` via ${i.via.trim()}` : "";
    const close = closeOf(i, featureSources);
    const note = close.stale ? (close.day ? `stale close ${close.day}` : "stale close") : null;
    const base = `${featureLabel(i.feature)} ${change ? change.text : number(i.value, 2)}${via}`;
    return [{ text: note ? `${base} (${note})` : base, stale: close.stale, note, title: close.stale ? staleTitle(close.day) : null }];
  });
  if (parts.length) return parts;
  if (typeof item.input !== "string" || !item.input.trim()) return null;
  // Only the backend's text: name any stale close among the inputs after it.
  const stale = inputs.flatMap((i) => {
    if (!isObject(i) || typeof i.feature !== "string") return [];
    const close = closeOf(i, featureSources);
    return close.stale ? [{ label: featureLabel(i.feature), day: close.day }] : [];
  });
  if (!stale.length) return [{ text: item.input.trim(), stale: false, note: null, title: null }];
  const note = `stale close: ${stale.map((x) => (x.day ? `${x.label} ${x.day}` : x.label)).join(", ")}`;
  return [
    {
      text: `${item.input.trim()} (${note})`,
      stale: true,
      note,
      title:
        stale.length === 1
          ? staleTitle(stale[0].day)
          : "Not the latest expected closes. They still count while their markets are closed, up to the configured maximum age.",
    },
  ];
}

// --- all markets --------------------------------------------------------------------------

const CLASS_GROUPS: [string, string][] = [
  ["crypto", "Crypto"],
  ["index", "Indices"],
  ["bond", "Rates"],
  ["fx", "FX"],
  ["commodity", "Commodities"],
  ["equity", "Equities"],
];

export type MarketGroup = { key: string; label: string; assets: Pick<Asset, "symbol" | "name">[] };

/**
 * Every delivered asset, grouped by asset class in a fixed order (unknown
 * classes after, in the order met), each keeping the API order: the index
 * that links every asset page from the dashboard.
 */
export function marketGroups(assets: Pick<Asset, "symbol" | "name" | "asset_class">[] | null | undefined): MarketGroup[] {
  const groups = new Map<string, MarketGroup>(CLASS_GROUPS.map(([key, label]) => [key, { key, label, assets: [] }]));
  const seen = new Set<string>();
  for (const a of assets ?? []) {
    if (!a || typeof a.symbol !== "string" || !a.symbol || seen.has(a.symbol)) continue;
    seen.add(a.symbol);
    const key = typeof a.asset_class === "string" && a.asset_class ? a.asset_class.toLowerCase() : "other";
    if (!groups.has(key)) {
      const words = key.replaceAll("_", " ");
      groups.set(key, { key, label: words[0].toUpperCase() + words.slice(1), assets: [] });
    }
    groups.get(key)!.assets.push({ symbol: a.symbol, name: a.name });
  }
  return [...groups.values()].filter((g) => g.assets.length);
}

// --- alerts -------------------------------------------------------------------------------

/** Alert priorities in display order; an unknown priority sorts after LOW. */
export const PRIORITY_RANK: Record<string, number> = { HIGH: 0, MEDIUM: 1, LOW: 2 };

/** Alerts shown in the dashboard panel (the Alerts page lists the rest). */
export const DASHBOARD_ALERTS = 12;

export type AlertRow = {
  alert: Alert;
  /** "RESOLVED 12:56" for a provider failure that has recovered since ("RESOLVED" when only the current status says so); null otherwise. */
  resolved: string | null;
};

function alertTime(alert: Pick<Alert, "created_at">): number | null {
  const t = Date.parse(String(alert.created_at ?? ""));
  return Number.isFinite(t) ? t : null;
}

/** Provider and status of a PROVIDER alert (its metrics); null for other alerts or incomplete metrics. */
function providerChange(alert: Alert): { provider: string; status: string } | null {
  if (alert.kind !== "PROVIDER" || !isObject(alert.metrics)) return null;
  const { provider, status } = alert.metrics;
  return typeof provider === "string" && provider && typeof status === "string" && status
    ? { provider, status }
    : null;
}

/** "12:56" on the failure's own day in Bucharest, else "Mon 28 Sep 12:56". */
function recoveryTime(failed: number, recovered: number): string {
  return bucharestDay(failed) === bucharestDay(recovered)
    ? bucharestHourMinute(recovered)
    : bucharestDateTime(recovered);
}

function byPriorityThenNewest(a: AlertRow, b: AlertRow): number {
  return (
    (PRIORITY_RANK[a.alert.priority] ?? 3) - (PRIORITY_RANK[b.alert.priority] ?? 3) ||
    (alertTime(b.alert) ?? 0) - (alertTime(a.alert) ?? 0)
  );
}

/**
 * The dashboard alerts panel. A provider failure alert is resolved once a
 * later alert for the same provider reports it back online (the badge gives
 * that time), or the current provider list shows it ONLINE. Open alerts come
 * first, by priority then newest; resolved ones follow, newest first, so an
 * old outage never sits above what is happening now and its recovery is not
 * pushed out of the list. At most `limit` rows.
 */
export function alertRows(alerts: Alert[], providers: unknown, limit = DASHBOARD_ALERTS): AlertRow[] {
  const online = new Set(
    (Array.isArray(providers) ? providers : [])
      .filter((p): p is { name: string; status: unknown } => isObject(p) && typeof p.name === "string")
      .filter((p) => p.status === "ONLINE")
      .map((p) => p.name),
  );
  const recoveries = new Map<string, number[]>();
  for (const alert of alerts) {
    const change = providerChange(alert);
    const t = alertTime(alert);
    if (change?.status !== "ONLINE" || t === null) continue;
    recoveries.set(change.provider, [...(recoveries.get(change.provider) ?? []), t]);
  }
  const rows: AlertRow[] = alerts.map((alert) => {
    const change = providerChange(alert);
    if (!change || change.status === "ONLINE") return { alert, resolved: null };
    const failed = alertTime(alert);
    const later = failed === null ? [] : (recoveries.get(change.provider) ?? []).filter((t) => t > failed);
    if (failed !== null && later.length)
      return { alert, resolved: `RESOLVED ${recoveryTime(failed, Math.min(...later))}` };
    return { alert, resolved: online.has(change.provider) ? "RESOLVED" : null };
  });
  const open = rows.filter((r) => !r.resolved).sort(byPriorityThenNewest);
  const resolved = rows
    .filter((r) => r.resolved)
    .sort((a, b) => (alertTime(b.alert) ?? 0) - (alertTime(a.alert) ?? 0));
  return [...open, ...resolved].slice(0, limit);
}
