import { number } from "@/lib/api";
import { ageText } from "@/lib/freshness";
import { zonedTime } from "@/lib/market-data";
import { Badge } from "@/components/ui/badge";
import { Panel } from "@/components/shared/panel";
import { isNum, signed } from "./signal";

/* GET /api/assets/{BTC|ETH} `positioning` (backend positioning.summarize); every field may be absent. */
type OptionTotals = {
  call_oi?: number | null;
  put_oi?: number | null;
  put_call_ratio?: number | null;
  oi_weighted_iv?: number | null;
};
type OptionExpiry = {
  expiry?: string;
  days_to_expiry?: number | null;
  skew?: { put_minus_call_iv?: number | null } | null;
};
type Perpetual = {
  basis_pct?: number | null;
  funding_8h?: number | null;
  open_interest?: number | null;
  open_interest_unit?: string | null;
};
type CrowdingFlag = { flag?: string };
export type PositioningSnapshot = {
  currency?: string;
  as_of?: string | null;
  age_seconds?: number | null;
  /** FRESH | STALE | MISSING */
  freshness_status?: string | null;
  options?: { totals?: OptionTotals | null; expiries?: OptionExpiry[] | null } | null;
  futures?: { perpetual?: Perpetual | null } | null;
  crowding?: CrowdingFlag[] | null;
  reason?: string | null;
};

/** Crowding flags (market.json positioning.crowding) in words. */
const FLAG_WORDS: Record<string, string> = {
  PERP_FUNDING_HIGH: "Perpetual funding high (longs crowded)",
  PERP_FUNDING_NEGATIVE: "Perpetual funding negative (shorts crowded)",
  BASIS_RICH: "Futures basis rich (longs crowded)",
  BASIS_BACKWARDATION: "Futures in backwardation (shorts crowded)",
  PUT_HEAVY_OI: "Put-heavy open interest (hedging demand)",
  CALL_HEAVY_OI: "Call-heavy open interest",
  PUT_SKEW_ELEVATED: "Put skew elevated (hedging demand)",
  CALL_SKEW_ELEVATED: "Call skew elevated",
};

function flagText(flag: string): string {
  if (FLAG_WORDS[flag]) return FLAG_WORDS[flag];
  const words = flag.toLowerCase().replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Signed percent with enough decimals for funding and basis ("+0.0100%"). */
function pct(value: unknown, digits = 4): string {
  return isNum(value) ? `${signed(value, digits, true)}%` : "Unavailable";
}

/** The snapshot has something to show (options or a perpetual). */
export function hasPositioning(p: PositioningSnapshot | null | undefined): p is PositioningSnapshot {
  return !!p && typeof p === "object" && (!!p.options || !!p.futures);
}

/**
 * Descriptive derivatives positioning (Deribit options and perpetual) for
 * BTC and ETH: put/call, implied volatility, open interest, basis and
 * funding, with its as-of time. It describes positioning; it never sets a
 * direction.
 */
export function Positioning({
  positioning: p,
  now,
}: {
  positioning: PositioningSnapshot;
  now: number;
}) {
  const totals = p.options?.totals ?? null;
  const front = (p.options?.expiries ?? []).find(
    (e) => isNum(e?.skew?.put_minus_call_iv) && (e.days_to_expiry ?? 0) >= 1,
  );
  const perp = p.futures?.perpetual ?? null;
  const asOf = zonedTime(p.as_of);
  const age = p.as_of ? now - Date.parse(p.as_of) : NaN;
  const status = p.freshness_status;
  const flags = (p.crowding ?? []).map((f) => f?.flag).filter((f): f is string => !!f);
  const rows: { label: string; value: string }[] = [
    {
      label: "Put/call open interest",
      value: isNum(totals?.put_call_ratio) ? number(totals.put_call_ratio, 2) : "Unavailable",
    },
    {
      label: "Option open interest",
      value:
        isNum(totals?.call_oi) || isNum(totals?.put_oi)
          ? `calls ${number(totals?.call_oi, 1)} · puts ${number(totals?.put_oi, 1)} (contracts)`
          : "Unavailable",
    },
    {
      label: "Implied volatility (OI-weighted)",
      value: isNum(totals?.oi_weighted_iv) ? `${number(totals.oi_weighted_iv, 1)}%` : "Unavailable",
    },
  ];
  if (front?.skew && isNum(front.skew.put_minus_call_iv))
    rows.push({
      label: "Skew, front expiry",
      value: `put minus call IV ${signed(front.skew.put_minus_call_iv, 2)} vol points`,
    });
  rows.push(
    { label: "Perpetual basis vs index", value: pct(perp?.basis_pct) },
    {
      label: "Perpetual funding (8 h)",
      value: isNum(perp?.funding_8h) ? pct(perp.funding_8h * 100) : "Unavailable",
    },
    {
      label: "Perpetual open interest",
      value: isNum(perp?.open_interest)
        ? `${number(perp.open_interest, 0)}${perp.open_interest_unit ? ` ${perp.open_interest_unit}` : ""}`
        : "Unavailable",
    },
    {
      label: "Crowding flags",
      value: flags.length ? flags.map(flagText).join(" · ") : "None crossed",
    },
  );
  return (
    <Panel
      title="Positioning (descriptive)"
      action={
        <span className="flex items-center gap-2 text-[11px] text-muted-foreground">
          {asOf ? `as of ${asOf}${Number.isFinite(age) ? ` · ${ageText(age)}` : ""}` : "time not recorded"}
          {status === "FRESH" ? (
            <Badge variant="positive">FRESH</Badge>
          ) : status === "STALE" ? (
            <Badge variant="negative">STALE</Badge>
          ) : null}
        </span>
      }
    >
      <dl className="detail-grid">
        {rows.map((r) => (
          <div key={r.label}>
            <dt>{r.label}</dt>
            <dd className="tabular-nums">{r.value}</dd>
          </div>
        ))}
      </dl>
      <p className="px-5 pb-4 text-[11px] text-muted-foreground">
        {p.reason ||
          "Open interest, skew and funding describe positioning; they are not a direction or a price target."}
        {status === "STALE" && " This snapshot is older than the positioning freshness limit, so it adds nothing to the score."}
      </p>
    </Panel>
  );
}
