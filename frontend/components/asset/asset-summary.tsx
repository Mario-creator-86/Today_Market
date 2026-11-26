import { ArrowDownRight, ArrowUpRight, Info, Minus } from "lucide-react";
import { Asset, number } from "@/lib/api";
import {
  asOfLine,
  basisText,
  dataChip,
  isStub,
  levelText,
  proxyLine,
  sessionBadge,
  signedChange,
} from "@/lib/market-data";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { StatusBadge } from "@/components/shared/status-badge";
import { cn } from "@/lib/utils";
import {
  isNum,
  signalLabel,
  signalTone,
  signed,
  signTone,
  toneText,
} from "./signal";

function Stat({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-1", className)}>
      <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="m-0 text-lg font-medium">{children}</dd>
    </div>
  );
}

/**
 * Top block of the asset page: the level, its change with the basis it is
 * measured against, the data chip (LIVE, DAILY CLOSE, DERIVED, PROXY, STALE,
 * LAST PRINT, STUB, NOT CONFIGURED, DISABLED) and, separately, the venue's
 * session state (MARKET OPEN, LAST CLOSE, …; never a freshness word), then the
 * current signal, score and evidence confidence. `.asset-price` is a test hook.
 */
export function AssetSummary({ asset }: { asset: Asset }) {
  const q = asset.quote;
  const d = asset.decision;
  const change = q ? signedChange(q) : null;
  const basis = q ? basisText(q) : "";
  const tone = signTone(change?.sign);
  const Arrow =
    tone === "positive"
      ? ArrowUpRight
      : tone === "negative"
        ? ArrowDownRight
        : Minus;
  const signal = signalLabel(d);
  const chip = dataChip(asset);
  // The venue's session state, apart from the chip's freshness verdict (never
  // STALE; left out when it would repeat the chip, LIVE next to LIVE).
  const state = sessionBadge(asset);
  return (
    <section
      aria-label={`${asset.symbol} price and signal`}
      className="mb-6 rounded-lg border border-border bg-card p-5"
    >
      <div className="asset-price">
        <strong>
          {q
            ? levelText(q.price, asset.asset_class, 5)
            : asset.proxy
              ? "No level · proxy only"
              : "Price unavailable"}
        </strong>
        {q && (
          <span
            className={cn(
              "inline-flex items-center gap-1 text-base font-medium",
              toneText[tone],
            )}
          >
            {change && <Arrow size={16} aria-hidden="true" />}
            {change ? change.text : "No change reported"}
            {change && basis && (
              <span className="text-xs font-normal text-muted-foreground">
                {basis}
              </span>
            )}
          </span>
        )}
        <Badge
          variant={chip.tone}
          className={cn(chip.prominent && "font-extrabold")}
        >
          {chip.label}
        </Badge>
        {state && <StatusBadge value={state} variant="outline" />}
        {isStub(q) && <StatusBadge value="DEVELOPMENT STUB" variant="negative" />}
        <span>
          {q
            ? asOfLine(q)
            : asset.proxy
              ? `${proxyLine(asset.proxy).text} · ${asOfLine(asset.proxy)}`
              : chip.detail || "No connected price source"}
        </span>
      </div>
      <dl className="mt-5 grid grid-cols-2 gap-4 border-t border-border pt-4 sm:grid-cols-4">
        <Stat label="Current signal">
          {signal ? (
            <StatusBadge
              value={signal}
              variant={signalTone(signal)}
              className="text-xs"
            />
          ) : (
            <span className="text-sm text-muted-foreground">
              No signal computed
            </span>
          )}
        </Stat>
        <Stat label="Decision score">
          <span className="flex items-center">
            <span className={toneText[signTone(d?.score)]}>
              {d && isNum(d.score) ? signed(d.score, 0) : "Unavailable"}
            </span>
            <span className="ml-1 text-xs text-muted-foreground">/ ±100</span>
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  className="ml-0.5 inline-flex size-6 cursor-help items-center justify-center rounded-sm text-muted-foreground hover:text-foreground"
                  aria-label="About the decision score"
                >
                  <Info size={13} aria-hidden="true" />
                </button>
              </TooltipTrigger>
              <TooltipContent>
                A signed −100 to +100 ranking of evidence strength and market
                confirmation. It is not a probability of success.
              </TooltipContent>
            </Tooltip>
          </span>
          <span className="block text-[11px] font-normal text-muted-foreground">
            Not a probability
          </span>
        </Stat>
        <Stat label="Evidence confidence">
          {d && isNum(d.evidence_confidence)
            ? `${number(d.evidence_confidence, 0)}%`
            : "Unavailable"}
        </Stat>
        <Stat label="Market confirmation">
          {d && isNum(d.market_confirmation)
            ? `${number(d.market_confirmation, 0)}%`
            : "Unavailable"}
        </Stat>
      </dl>
    </section>
  );
}
