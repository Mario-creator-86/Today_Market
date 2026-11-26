import Link from "next/link";
import {
  Asset,
  AssetCoverage,
  AssetProxy,
  DataNotice,
  number,
  Provider,
  Quote,
} from "@/lib/api";
import {
  asOfLabel,
  changeLine,
  coverageText,
  dataChip,
  disabledSources,
  hasClockTime,
  isDailyClass,
  isDerived,
  isOfficialPublisher,
  levelText,
  missingKeysText,
  proxyStale,
  readableLabel,
  sessionBasisText,
  sessionLabel,
  shortDate,
  sourceName,
  sourceText,
  venueName,
  zonedTime,
} from "@/lib/market-data";
import { Badge } from "@/components/ui/badge";
import { CardContent } from "@/components/ui/card";
import { DataNotices } from "@/components/shared/data-notices";
import { Panel } from "@/components/shared/panel";
import { isNum } from "./signal";

type Row = { label: string; value: React.ReactNode };

function Rows({ rows }: { rows: Row[] }) {
  return (
    <dl className="detail-grid">
      {rows.map((r) => (
        <div key={r.label}>
          <dt>{r.label}</dt>
          <dd>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * "Official daily close · latest expected publication" for official publishers
 * only; a daily value from a trading feed says what it is (an IEX last trade
 * is not the exchange's official close). "Live feed" otherwise.
 */
function dataClassText(q: Quote): string {
  if (isDerived(q) && isDailyClass(q))
    return "Daily value derived by this app (not an official index value)";
  if (isDailyClass(q)) {
    const status =
      q.close_status === "CURRENT"
        ? "latest expected publication"
        : q.close_status === "STALE"
          ? "late: a newer publication was expected"
          : "publication status not reported";
    if (isOfficialPublisher(q.provider)) return `Official daily close · ${status}`;
    return q.feed
      ? `Last ${q.feed.toUpperCase()} trade of the session, not the official exchange close · ${status}`
      : `Session close reported by ${sourceName(q)}, not an official publication · ${status}`;
  }
  return "Live feed";
}

function previousText(
  q: Pick<Quote, "previous_close" | "previous_close_at" | "change_basis">,
  assetClass: string,
): string | null {
  if (!isNum(q.previous_close)) return null;
  const day = shortDate(q.previous_close_at);
  const what = q.change_basis === "previous_fix" ? "Previous fix" : "Previous close";
  return `${what} ${levelText(q.previous_close, assetClass, 6)}${day ? ` (${day})` : ""}`;
}

/** The source as its attribution names it; the provider id stays available as a tooltip. */
function Source({ item, extra }: { item: Quote | AssetProxy; extra?: string | null }) {
  return (
    <span title={`Provider: ${sourceText(item)}`}>
      {[sourceName(item), extra].filter(Boolean).join(" · ")}
    </span>
  );
}

/**
 * One instant, told once: the service's as-of text (in its own zone, without
 * the session state the Session row gives), plus the timestamp in the app
 * zone only when that text names no clock time ("close Fri 25 Sep").
 */
function asOfRow(q: Quote): string {
  const label = asOfLabel(q);
  const time = zonedTime(q.timestamp);
  if (label && (hasClockTime(label) || !time)) return label;
  return [label, time ?? "Time not recorded"].filter(Boolean).join(" · ");
}

function QuoteRows({ asset, quote: q }: { asset: Asset; quote: Quote }) {
  const chip = dataChip(asset);
  const change = changeLine(q);
  const previous = previousText(q, asset.asset_class);
  const session = asset.session;
  const rows: Row[] = [
    { label: "Instrument", value: q.label || asset.name },
    {
      label: "Data",
      value: (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <Badge variant={chip.tone} className={chip.prominent ? "font-extrabold" : undefined}>
            {chip.label}
          </Badge>
          {dataClassText(q)}
        </span>
      ),
    },
    { label: "Source", value: <Source item={q} /> },
    { label: "As of", value: asOfRow(q) },
    {
      label: "Change",
      value: [change?.text ?? "No change reported", previous]
        .filter(Boolean)
        .join(" · "),
    },
  ];
  if (q.delay_label) rows.push({ label: "Delay", value: readableLabel(q.delay_label) });
  rows.push({
    label: "Session",
    value: [
      sessionLabel(asset),
      // The service's own name for the venue (config market_sessions), else the local fallback map.
      session?.venue_label || venueName(session?.venue),
      sessionBasisText(session?.basis),
    ]
      .filter(Boolean)
      .join(" · "),
  });
  return <Rows rows={rows} />;
}

function ProxyBlock({ proxy: p, symbol }: { proxy: AssetProxy; symbol: string }) {
  const change = changeLine(p);
  // The last print of a closed session is not a late quote (see proxyStale).
  const status = [
    p.market_status,
    proxyStale(p) ? "STALE" : p.freshness_status === "FRESH" ? "FRESH" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="provenance-proxy" aria-label={`Proxy for ${symbol}`} role="group">
      <div className="flex flex-wrap items-center gap-2 px-5 pt-4">
        <Badge variant="neutral">PROXY</Badge>
        <strong className="text-sm font-medium">
          {p.label || `${p.symbol} (proxy for ${symbol})`}
        </strong>
      </div>
      {p.caveat && (
        <p className="px-5 pt-2 text-xs text-muted-foreground">{p.caveat}</p>
      )}
      <Rows
        rows={[
          {
            label: "Proxy price",
            value: `${p.symbol} ${number(p.price, 4)} (not the ${symbol} level)`,
          },
          { label: "Proxy change", value: change?.text ?? "No change reported" },
          {
            label: "Source",
            value: <Source item={p} extra={p.delay_label ? readableLabel(p.delay_label) : null} />,
          },
          {
            label: "As of",
            value: `${zonedTime(p.timestamp) ?? "Time not recorded"}${status ? ` · ${status}` : ""}`,
          },
        ]}
      />
      <p className="px-5 pb-4 text-xs text-muted-foreground">
        Direction only. A proxy is never shown as the {symbol} level and never
        makes a setup actionable.
      </p>
    </div>
  );
}

/** Provider health row (dashboard `providers`); `label` is the configured human name when the service sends one. */
export type ProviderHealth = Pick<Provider, "name" | "status" | "error"> & {
  label?: string | null;
};

/**
 * One configured source of the asset with its role and the health of the
 * adapter behind it (backend coverage `sources`): `state` READY | MISSING_KEY |
 * DISABLED | UNAVAILABLE, `health` the provider status (e.g. AUTH_ERROR).
 */
type CoverageSource = {
  name: string;
  label?: string | null;
  role?: "level" | "proxy" | string | null;
  state?: string | null;
  health?: string | null;
  error?: string | null;
};

/** Coverage with the level and proxy sources apart, when the service sends them. */
type CoverageDetail = AssetCoverage & {
  level_providers?: string[] | null;
  proxy_providers?: string[] | null;
  sources?: CoverageSource[] | null;
};

/** Health states in which a source is not delivering (backend metrics.FAILURE_STATUSES and timeouts). */
const FAILING: Record<string, string> = {
  AUTH_ERROR: "authentication rejected",
  RATE_LIMITED: "rate limited",
  OFFLINE: "offline",
  DEGRADED: "degraded",
  TIMEOUT: "timed out",
  UNAVAILABLE: "unavailable",
};

function names(list: unknown): string[] {
  return Array.isArray(list)
    ? [...new Set(list.filter((s): s is string => typeof s === "string" && !!s))]
    : [];
}

type SourceRow = { text: string; failing: boolean };

/** "Massive", or "Massive — Authentication rejected (HTTP 403)" while its health says it is failing. */
function statusRow(label: string, status: unknown, error: unknown): SourceRow {
  const problem = typeof status === "string" ? FAILING[status] : undefined;
  if (!problem) return { text: label, failing: false };
  return {
    text: `${label} — ${typeof error === "string" && error.trim() ? error.trim() : problem}`,
    failing: true,
  };
}

/**
 * Fallback for a service without coverage `sources`: the dashboard health row
 * of the same name (a source served by a differently named adapter is not
 * found there, so it is never flagged).
 */
function sourceStatus(name: string, health: ProviderHealth[]): SourceRow {
  const row = health.find((h) => h.name?.toLowerCase() === name.toLowerCase());
  return statusRow(row?.label || name, row?.status, row?.error);
}

type SourceGroup = { title: string; rows: SourceRow[] };

/** "Level source" / "Level sources". */
function plural(one: string, many: string, count: number): string {
  return count === 1 ? one : many;
}

/**
 * The asset's usable (READY) sources: from coverage `sources`, which carries
 * each source's role and the health of the adapter behind it; else the
 * level/proxy lists or the merged list, checked against the dashboard health.
 */
function sourceGroups(c: CoverageDetail | null | undefined, health: ProviderHealth[]): SourceGroup[] {
  const detail = Array.isArray(c?.sources)
    ? c.sources.filter(
        (s): s is CoverageSource =>
          !!s && typeof s === "object" && typeof s.name === "string" && !!s.name,
      )
    : null;
  if (detail) {
    const usable = detail.filter((s) => !s.state || s.state === "READY");
    const rows = (role: string) =>
      usable
        .filter((s) => (role === "proxy" ? s.role === "proxy" : s.role !== "proxy"))
        .map((s) => statusRow(s.label?.trim() || s.name, s.health, s.error));
    const level = rows("level");
    const proxy = rows("proxy");
    return [
      { title: plural("Level source", "Level sources", level.length), rows: level },
      { title: plural("Direction proxy", "Direction proxies", proxy.length), rows: proxy },
    ];
  }
  const split = Array.isArray(c?.level_providers) || Array.isArray(c?.proxy_providers);
  if (split) {
    const level = names(c?.level_providers).map((n) => sourceStatus(n, health));
    const proxy = names(c?.proxy_providers).map((n) => sourceStatus(n, health));
    return [
      { title: plural("Level source", "Level sources", level.length), rows: level },
      { title: plural("Direction proxy", "Direction proxies", proxy.length), rows: proxy },
    ];
  }
  return [
    {
      title: "Configured sources (level or proxy)",
      rows: names(c?.providers).map((n) => sourceStatus(n, health)),
    },
  ];
}

function CoverageBlock({
  asset,
  providers: health,
}: {
  asset: Asset;
  providers: ProviderHealth[];
}) {
  const c = asset.coverage as CoverageDetail | null | undefined;
  // A missing key may add a live feed, a daily close or a proxy: worded neutrally.
  const keys = missingKeysText(c?.missing_keys);
  const off = disabledSources(c);
  const groups = sourceGroups(c, health);
  const failing = groups.some((g) => g.rows.some((r) => r.failing));
  const title =
    coverageText(c) ??
    (asset.quote || asset.proxy ? null : "No price source delivered data");
  return (
    <CardContent className="provenance-coverage grid gap-1.5 text-sm">
      {title && <strong className="font-medium">{title}</strong>}
      {keys && (
        <p className="text-muted-foreground">
          {asset.quote ? "Another source" : `A source for ${asset.symbol}`} {keys}.
        </p>
      )}
      {off.length > 0 && (
        <p className="text-muted-foreground">
          Switched off in Settings: {off.join(", ")}.
        </p>
      )}
      {groups
        .filter((g) => g.rows.length > 0)
        .map((g) => (
          <p key={g.title} className="text-muted-foreground">
            {g.title}:{" "}
            {g.rows.map((r, i) => (
              <span key={i} className={r.failing ? "text-warning" : undefined}>
                {i > 0 && ", "}
                {r.text}
              </span>
            ))}
            .
          </p>
        ))}
      {/* A link only where something can be done: a missing key, a source to switch back on, a failing source. */}
      {keys && (
        <Link href="/settings" className="text-primary underline-offset-2 hover:underline">
          Configure in Settings → Data providers
        </Link>
      )}
      {!keys && off.length > 0 && (
        <Link href="/settings" className="text-primary underline-offset-2 hover:underline">
          Switch on in Settings → Data providers
        </Link>
      )}
      {!keys && !off.length && failing && (
        <Link href="/settings" className="text-primary underline-offset-2 hover:underline">
          Check in Settings → Data providers
        </Link>
      )}
    </CardContent>
  );
}

/**
 * Where the asset's numbers come from (docs/MARKET_COVERAGE.md): source,
 * as-of, change basis and data class of the level; the labelled proxy with
 * its caveat; when a source is missing, which key would add one, and which
 * configured source is failing; and the notices its providers' terms require
 * (`notices`, already filtered to them).
 */
export function DataProvenance({
  asset,
  notices = [],
  providers = [],
}: {
  asset: Asset;
  notices?: DataNotice[];
  /** Provider health (dashboard), to say when a source of this asset is failing. */
  providers?: ProviderHealth[];
}) {
  const q = asset.quote;
  const p = asset.proxy;
  const c = asset.coverage;
  const showCoverage =
    (!!c && c.status !== "OK") || (!q && !p) || disabledSources(c).length > 0;
  return (
    <Panel title="Data source">
      {q && <QuoteRows asset={asset} quote={q} />}
      {p && <ProxyBlock proxy={p} symbol={asset.symbol} />}
      {showCoverage && <CoverageBlock asset={asset} providers={providers} />}
      {notices.length > 0 && (
        <CardContent className="provenance-notices border-t border-border pt-3 pb-4">
          <DataNotices notices={notices} />
        </CardContent>
      )}
    </Panel>
  );
}
