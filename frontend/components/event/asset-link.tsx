"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, type Dashboard } from "@/lib/api";
import { cn } from "@/lib/utils";

const NONE: ReadonlySet<string> = new Set();

function symbolsOf(data: Dashboard): ReadonlySet<string> {
  return new Set((data.assets ?? []).map((a) => a.symbol).filter(Boolean));
}

/**
 * Symbols with an asset page: the registry the shell already loads with the
 * dashboard (same query key, so no extra request). Empty until it arrives.
 */
export function useTrackedSymbols(): ReadonlySet<string> {
  const { data } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
    select: symbolsOf,
  });
  return data ?? NONE;
}

/**
 * Classes for a symbol link that sits over a larger link (an event row whose
 * headline link covers the row): an invisible hit area 6 px beyond the text
 * on every side (a 3-letter symbol in 11 px text becomes about 30 x 27 px), so
 * a near miss on a phone still opens the asset, without changing the line.
 * The link takes pointer events inside a container that passes them through
 * (`pointer-events-none`), so the gaps between symbols still open the row.
 */
export const SYMBOL_TAP_AREA =
  "pointer-events-auto relative after:absolute after:inset-[-6px] after:content-['']";

/** Spacing around the " / " between symbols with SYMBOL_TAP_AREA: neighbouring hit areas never overlap. */
export const SYMBOL_TAP_GAP = "mx-1";

/** A symbol linked to its asset page when the asset is tracked; plain text otherwise. */
export function AssetSymbol({
  symbol,
  tracked,
  className,
  linkClassName,
}: {
  symbol: string;
  tracked: ReadonlySet<string>;
  className?: string;
  /** Extra classes for the link only (e.g. SYMBOL_TAP_AREA); plain-text symbols ignore them. */
  linkClassName?: string;
}) {
  return tracked.has(symbol) ? (
    <Link
      className={cn("source-link", className, linkClassName)}
      href={`/assets/${encodeURIComponent(symbol)}`}
    >
      {symbol}
    </Link>
  ) : (
    <span className={className}>{symbol}</span>
  );
}

/** "BTC / ETH" with each tracked symbol linked. */
export function AssetSymbols({
  symbols,
  tracked,
  className,
  linkClassName,
  separatorClassName,
}: {
  symbols: string[];
  tracked: ReadonlySet<string>;
  className?: string;
  /** Extra classes for the links only (see AssetSymbol). */
  linkClassName?: string;
  /** Classes for the " / " between symbols, e.g. SYMBOL_TAP_GAP. */
  separatorClassName?: string;
}) {
  return (
    <>
      {symbols.map((symbol, i) => (
        <span key={symbol}>
          {i > 0 &&
            (separatorClassName ? (
              <span className={separatorClassName}> / </span>
            ) : (
              " / "
            ))}
          <AssetSymbol
            symbol={symbol}
            tracked={tracked}
            className={className}
            linkClassName={linkClassName}
          />
        </span>
      ))}
    </>
  );
}
