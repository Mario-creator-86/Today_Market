/*
 * Asset route symbols. Pure, without imports, so the vitest suite
 * (lib/market-data.test.ts) can collect it.
 */

/** The route segment as text: percent-decoded once (a malformed escape is kept as sent). */
export function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** Tracked symbols are upper-case: "btc" and "BTC%20" both mean "BTC". */
export function canonicalSymbol(segment: string): string {
  return decodeSegment(segment).trim().toUpperCase();
}

/** Could name a tracked asset (letters, digits, "." "_" "-"); anything else is not asked for. */
export function isAssetSymbol(symbol: string): boolean {
  return /^[A-Z0-9][A-Z0-9._-]{0,31}$/.test(symbol);
}

/**
 * The symbol a route segment names exactly (already canonical: "BTC"), else
 * null. Unlike canonicalSymbol it never repairs the segment, so it gives the
 * same answer however often the segment was percent-decoded before it got
 * here: "BTC%2520" (the page's view) and "BTC%20" (the metadata's view of the
 * same URL) are both null, while "BTC%20" and "btc" as typed are redirected by
 * the page anyway.
 */
export function exactSymbol(segment: string): string | null {
  const symbol = canonicalSymbol(segment);
  return isAssetSymbol(symbol) && symbol === decodeSegment(segment) ? symbol : null;
}
