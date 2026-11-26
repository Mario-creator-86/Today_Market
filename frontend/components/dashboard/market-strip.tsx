import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { StripItem, tileView } from "./selectors";

function Tile({ item }: { item: StripItem }) {
  const view = tileView(item);
  const { move, tag } = view;
  return (
    <Link
      href={`/assets/${encodeURIComponent(item.symbol)}`}
      className="market-tile"
      title={view.title}
      data-chip={view.chip.label}
    >
      <div className="market-tile-head">
        <strong>{item.label}</strong>
        {/* Local freshness checks (currentAsset) may have downgraded a live quote to STALE. */}
        <Badge
          variant={tag.tone}
          className={cn(
            "market-chip px-1 py-px text-[10px] leading-tight",
            tag.prominent && "font-extrabold ring-1 ring-destructive/60",
          )}
        >
          {tag.label}
        </Badge>
      </div>
      <div className="market-tile-value">
        <b>{view.headline}</b>
        {move && (
          <span className={cn(move.sign === -1 && "down", move.sign === 1 && "text-positive", move.sign === 0 && "muted")}>
            {move.text}
          </span>
        )}
      </div>
    </Link>
  );
}

/**
 * MARKETS, the compact strip (§27): the configured symbols in order, each
 * with its level, the change coloured by sign and ONE tag saying what the
 * value is (LIVE, CLOSE <day>, DERIVED <day>, PROXY via <instrument>, STALE,
 * LAST PRINT, STUB, NOT CONFIGURED, DISABLED, NO DATA YET; §46/§49). A proxy
 * never supplies a level, only the direction. Everything else (the change
 * basis, the session, the as-of time and source, the proxy's caveat, missing
 * keys) is the tile's tooltip and is on the asset page. Expects assets
 * already passed through `currentAsset`.
 */
export function MarketStrip({ items }: { items: StripItem[] }) {
  return (
    <section className="market-strip" aria-label="Market strip">
      {items.map((item) => (
        <Tile key={item.symbol} item={item} />
      ))}
    </section>
  );
}
