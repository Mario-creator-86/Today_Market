import Link from "next/link";
import { Clock3 } from "lucide-react";
import {
  Asset,
  Dashboard,
  EventRisk,
  Interpretation,
  localTime,
  MarketViewItem,
  number,
  Regime,
  RegimeDriver,
} from "@/lib/api";
import { closedMarkets, interpretationSource, staleText, viewItems } from "@/lib/event-risk";
import {
  APP_ZONE,
  gapKind,
  missingKeysText,
  type GapKind,
} from "@/lib/market-data";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import {
  basisTag,
  driverBasisWord,
  heroText,
  heroWarning,
  measuredDrivers,
  regimeHeadline,
} from "./opening";
import { driverInputParts, featureLabel, type DriverInputPart } from "./view-model";

function ActivityIcon() {
  return (
    <svg
      width="30"
      height="30"
      viewBox="0 0 30 30"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M2 20h6l4-12 5 17 4-11h7"
        stroke="currentColor"
        strokeWidth="2"
      />
    </svg>
  );
}

const ARROW: Record<string, string> = { up: "↑", down: "↓", flat: "→" };
const WORD: Record<string, string> = { up: "up", down: "down", flat: "flat" };

/** Display order and wording of the reasons an input is missing. */
const GAPS: { kind: GapKind; heading: string; short: string }[] = [
  { kind: "not_configured", heading: "Not configured", short: "not configured" },
  { kind: "disabled", heading: "Switched off", short: "switched off" },
  { kind: "no_data", heading: "No data yet", short: "no data yet" },
  { kind: "unusable", heading: "Stale or not usable", short: "stale or not usable" },
  { kind: "unknown", heading: "Missing", short: "unavailable" },
];

type Missing = { labels: Record<GapKind, string[]>; keys: string[] };

/**
 * Split missing inputs by why they are missing, using each asset's coverage:
 * no source configured (with the keys that would add one), sources switched
 * off, configured but without data yet, or data that exists but is stale or
 * not usable (an old close, an old proxy). "No data yet" is only said when the
 * asset really has none. Features without coverage information stay "Missing".
 */
function groupMissing(features: string[], assets: Asset[]): Missing {
  const bySymbol = new Map(assets.map((a) => [a.symbol, a]));
  const out: Missing = {
    labels: { not_configured: [], disabled: [], no_data: [], unusable: [], unknown: [] },
    keys: [],
  };
  for (const feature of features) {
    const symbol = feature.includes(":") ? "" : feature;
    const coverage = bySymbol.get(symbol)?.coverage;
    const kind = gapKind(coverage);
    out.labels[kind].push(featureLabel(feature));
    if (kind === "not_configured") out.keys.push(...(coverage?.missing_keys ?? []));
  }
  out.keys = [...new Set(out.keys)];
  return out;
}

/** Missing features grouped by reason, for a tooltip: "Not configured: NASDAQ; Stale or not usable: S&P 500". */
function gapTitle(features: string[], assets: Asset[]): string {
  const g = groupMissing(features, assets);
  return GAPS.filter((x) => g.labels[x.kind].length)
    .map((x) => `${x.heading}: ${g.labels[x.kind].join(", ")}`)
    .join("; ");
}

/**
 * Why a driver has no arrow: the reason its inputs are missing ("not
 * configured", "switched off", "no data yet", "stale or not usable"); mixed
 * reasons are listed together, and inputs without coverage are "Unavailable".
 */
function driverGap(item: RegimeDriver, assets: Asset[]): string {
  const missing = (item.missing ?? []).filter((m) => typeof m === "string");
  if (!missing.length) return "Unavailable";
  const g = groupMissing(missing, assets);
  const reasons = GAPS.filter((x) => g.labels[x.kind].length);
  if (reasons.length === 1 && reasons[0].kind === "unknown") return "Unavailable";
  return reasons.map((x) => x.short).join(" / ");
}

/**
 * The inputs that set the arrow ("US2Y -6 bp, US10Y -1 bp"). A close the
 * backend marks STALE (not the latest expected close) keeps its vote but is
 * flagged in the warning colour with its date ("VIX -4.44% (stale close
 * 22 Sep)") and a tooltip saying what that means, so an old close never
 * reads as a current move.
 */
function InputText({ parts }: { parts: DriverInputPart[] }) {
  return (
    <span className="font-mono tabular-nums">
      {parts.map((p, i) => (
        <span key={`${i}-${p.text}`}>
          {i > 0 && ", "}
          {p.stale ? (
            <span className="text-warning" title={p.title ?? undefined}>
              {p.text}
            </span>
          ) : (
            p.text
          )}
        </span>
      ))}
    </span>
  );
}

/**
 * One driver with the inputs that set its arrow, e.g. "Rates ↓ US2Y -6 bp · last close" (listed under "Data
 * coverage and basis"). Unmeasured drivers say why instead of showing an arrow; observations read from direction
 * proxies say "via proxies", never "live".
 */
function Driver({
  item,
  assets,
  sources,
}: {
  item: RegimeDriver;
  assets: Asset[];
  /** regime.feature_sources: per-feature close status when the input itself does not carry it. */
  sources: unknown;
}) {
  const arrow = ARROW[item.direction];
  const basis = driverBasisWord(item);
  const input = driverInputParts(item, sources);
  return (
    <li
      className="flex flex-wrap items-center gap-1.5 text-xs"
      title={
        item.missing?.length
          ? gapTitle(
              item.missing.filter((m) => typeof m === "string"),
              assets,
            )
          : undefined
      }
    >
      <span className="text-muted-foreground">{item.driver}</span>
      {arrow ? (
        <b
          aria-hidden="true"
          className={cn(
            "text-sm font-semibold text-foreground",
            item.direction === "flat" && "text-muted-foreground",
          )}
        >
          {arrow}
        </b>
      ) : (
        <span className="text-[10px] text-muted-foreground">
          {driverGap(item, assets)}
        </span>
      )}
      {arrow && <span className="sr-only">{WORD[item.direction]}</span>}
      {input && <InputText parts={input} />}
      {basis && (input || arrow) && (
        <span className="text-[10px] text-muted-foreground">· {basis}</span>
      )}
    </li>
  );
}

/** INSUFFICIENT_DATA: which required inputs are missing, and why. */
function MissingInputs({ regime, assets }: { regime: Regime; assets: Asset[] }) {
  const features = (regime.missing_required ?? []).filter(
    (f): f is string => typeof f === "string" && !!f,
  );
  if (!features.length) return null;
  const g = groupMissing(features, assets);
  const keys = missingKeysText(g.keys);
  return (
    <ul className="regime-missing" aria-label="Missing regime inputs">
      {GAPS.filter((x) => g.labels[x.kind].length).map((x) => (
        <li key={x.kind}>
          <strong>{x.heading}:</strong> {g.labels[x.kind].join(", ")}
          {x.kind === "not_configured" && keys && ` (${keys})`}
          {x.kind === "not_configured" && keys && (
            <>
              {" · "}
              <Link href="/settings" className="text-primary underline-offset-2 hover:underline">
                Configure in Settings → Data providers
              </Link>
            </>
          )}
          {x.kind === "disabled" && (
            <>
              {" · "}
              <Link href="/settings" className="text-primary underline-offset-2 hover:underline">
                Switch on in Settings → Data providers
              </Link>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * One driver chip of the hero: the name and its direction arrow ("Rates ↓"). The inputs behind the arrow are the
 * tooltip and are listed under "Data coverage and basis". A driver without a measurement says why; a late close
 * is flagged; a chip of current observations names what they are ("live", "via proxies").
 */
function DriverChip({
  item,
  assets,
  sources,
  showBasis,
}: {
  item: RegimeDriver;
  assets: Asset[];
  sources: unknown;
  /** Current observations name their basis on the chip; the regime's own drivers share the hero's basis tag. */
  showBasis: boolean;
}) {
  const arrow = ARROW[item.direction];
  const basis = driverBasisWord(item);
  const input = driverInputParts(item, sources);
  const stale = !!input?.some((p) => p.stale);
  const missing = (item.missing ?? []).filter((m) => typeof m === "string");
  const title = [
    input ? [input.map((p) => p.text).join(", "), basis].filter(Boolean).join(" · ") : null,
    missing.length ? gapTitle(missing, assets) : null,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <li className="now-chip" title={title || undefined} data-direction={arrow ? item.direction : "unavailable"}>
      <span className="text-muted-foreground">{item.driver}</span>
      {arrow ? (
        <>
          <b aria-hidden="true" className={cn(item.direction === "flat" && "text-muted-foreground")}>
            {arrow}
          </b>
          <span className="sr-only">{WORD[item.direction]}</span>
        </>
      ) : (
        <span className="text-[10px] text-muted-foreground">{driverGap(item, assets)}</span>
      )}
      {arrow && showBasis && basis && <span className="text-[10px] text-muted-foreground">{basis}</span>}
      {stale && <span className="text-[10px] text-warning">stale close</span>}
    </li>
  );
}

/**
 * NOW, the hero of the opening screen: the regime in plain words with ONE basis tag ("live" / "from Thursday's
 * close"), the interpretation's view in at most two sentences with its source as ONE small tag (AI or
 * Deterministic), the driver arrows on one line and at most ONE warning line. Numbers, the basis label, the
 * rule-coverage confidence (never a probability) and every conflict are under "Data coverage and basis".
 */
export function RegimeBanner({
  dashboard: d,
  interpretation,
  risk,
  now,
}: {
  dashboard?: Dashboard;
  /** Stored market interpretation; its view sits under the headline. */
  interpretation?: Interpretation | null;
  /** The event layer: what EVENTS and POSITIONING already state is not repeated in the view. */
  risk?: EventRisk | null;
  /** Client clock (useClock), for the interpretation's age. */
  now: number;
}) {
  const regime = d?.regime;
  const regimes = d?.regimes ?? [];
  const insufficient = !regimes.length && regime?.posture === "INSUFFICIENT_DATA";
  const drivers = (regime?.drivers ?? []).filter(
    (x) => x && typeof x.driver === "string",
  );
  const assets = d?.assets ?? [];
  const tag = regimes.length ? basisTag(regime) : null;
  const historical = regime?.basis === "last_close";
  const currentDrivers = measuredDrivers(regime?.current_drivers);
  const divergences = regime?.divergences ?? [];
  const featureSources = (regime as { feature_sources?: unknown } | undefined)?.feature_sources;
  const basisLabel =
    regime?.basis_label ||
    (regime?.basis === "last_close"
      ? "last close, markets closed"
      : regime?.basis === "live"
        ? "live observations"
        : null);
  const observations =
    regime?.basis === "last_close" ? "last-close" : "fresh";
  const source = interpretation ? interpretationSource(interpretation) : null;
  const stale = interpretation ? staleText(interpretation, now) : null;
  const view = interpretation
    ? heroText(interpretation.output?.ai_view, {
        eventNames: [risk?.next_event?.name, risk?.dominant_event?.name].filter(
          (n): n is string => typeof n === "string",
        ),
        symbols: risk
          ? [risk.favored, risk.pressured, risk.non_confirming].flatMap((group) =>
              viewItems<MarketViewItem>(group).map((item) => item.symbol),
            )
          : [],
        closedNoteShown: !!risk && closedMarkets(risk.markets_open).length > 0,
      })
    : "";
  const warning = regimes.length ? heroWarning(regime) : null;
  return (
    // Phone and tablet layouts live in globals.css and key on this structure:
    // .regime > .regime-icon + (text column) + .regime-asof, in this order.
    <section className="regime" aria-labelledby="regime-heading">
      <div className="regime-icon">
        <ActivityIcon />
      </div>
      <div className="min-w-0">
        <div className="now-eyebrow">
          <span>Market now</span>
          {tag && (
            <Badge
              variant={tag.historical ? "neutral" : "positive"}
              title={tag.title ?? undefined}
              data-basis={regime?.basis}
            >
              {tag.label}
            </Badge>
          )}
          {source && (
            <Badge
              variant="outline"
              title={source.label}
              data-source={source.deterministic ? "deterministic" : "ai"}
            >
              {source.deterministic ? "Deterministic" : "AI"}
            </Badge>
          )}
          {stale && <Badge variant="neutral">{stale}</Badge>}
        </div>
        <h2 id="regime-heading">
          {regimeHeadline(regimes, regime?.posture) ?? (insufficient ? "Insufficient data" : "Awaiting evidence")}
        </h2>
        {view && (
          <p className="regime-ai-text" role="note" aria-label="Market view">
            {view}
          </p>
        )}
        {!regimes.length && (
          <p>
            {insufficient && regime?.missing_required?.length
              ? "No regime rule has all of its required inputs."
              : "A regime requires fresh cross-asset observations."}
          </p>
        )}
        {insufficient && regime && (
          <MissingInputs regime={regime} assets={assets} />
        )}
        {drivers.length > 0 && (
          <div className="now-drivers">
            {currentDrivers.length > 0 && <span className="now-row-label">{historical ? "Close" : "Regime"}</span>}
            <ul className="now-chips" aria-label="Driver directions">
              {drivers.map((item) => (
                <DriverChip key={item.driver} item={item} assets={assets} sources={featureSources} showBasis={false} />
              ))}
            </ul>
          </div>
        )}
        {currentDrivers.length > 0 && (
          <div className="now-drivers">
            <span className="now-row-label">Today</span>
            <ul className="now-chips" aria-label="Current directions">
              {currentDrivers.map((item) => (
                <DriverChip key={item.driver} item={item} assets={assets} sources={undefined} showBasis />
              ))}
            </ul>
          </div>
        )}
        {warning && (
          <p
            className="now-warning"
            role={warning.kind === "conflict" ? "note" : undefined}
            aria-label={warning.kind === "conflict" ? "Current evidence conflicts" : undefined}
          >
            {warning.text}
            {warning.more > 0 && ` (+${warning.more} more under Data coverage and basis)`}
          </p>
        )}
        {(regimes.length > 0 || drivers.length > 0) && (
          <details className="regime-data">
            <summary>Data coverage and basis</summary>
            {basisLabel && <p className="regime-basis">Basis: {basisLabel}</p>}
            {regimes.length > 0 && (
              <p>
                {regime?.confidence != null
                  ? `Confidence ${number(regime.confidence, 0)}% — rule coverage across ${observations} observations (${number(regime.coverage_pct, 0)}% of inputs connected). Not a probability.`
                  : "Observed conditions across the connected markets."}
              </p>
            )}
            {drivers.length > 0 && (
              <>
                <b className="regime-kicker">{historical ? "Historical inputs" : "Why now"}</b>
                <ul className="mt-1.5 grid gap-1" aria-label="Regime drivers">
                  {drivers.map((item) => (
                    <Driver key={item.driver} item={item} assets={assets} sources={featureSources} />
                  ))}
                </ul>
              </>
            )}
            {currentDrivers.length > 0 && (
              <>
                <b className="regime-kicker">Current observations</b>
                <ul className="mt-1.5 grid gap-1" aria-label="Current market observations">
                  {currentDrivers.map((item) => (
                    <Driver key={item.driver} item={item} assets={assets} sources={undefined} />
                  ))}
                </ul>
              </>
            )}
            {historical && (
              <p className="mt-2">
                Latest published closes describe historical conditions. Current observations use their own periods and
                instruments; direction proxies do not establish an executable signal.
              </p>
            )}
            {divergences.length > 0 && (
              <ul className="mt-2 grid gap-1 text-xs text-warning" aria-label="All conflicts with the historical context">
                {divergences.map((item) => (
                  <li key={item.feature}>{item.text}</li>
                ))}
              </ul>
            )}
          </details>
        )}
      </div>
      <div className="regime-asof">
        <Clock3 size={15} aria-hidden="true" />
        <span>
          As of {localTime(d?.as_of)}
          {d?.as_of && Number.isFinite(Date.parse(d.as_of)) ? ` ${APP_ZONE}` : ""}
        </span>
      </div>
    </section>
  );
}
