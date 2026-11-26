import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { AssetView } from "@/components/asset/asset-view";
import {
  canonicalSymbol,
  decodeSegment,
  exactSymbol,
  isAssetSymbol,
} from "@/components/asset/symbol";

type Props = { params: Promise<{ symbol: string }> };

/**
 * The symbol until the page knows the asset's name (AssetView then sets
 * "Bitcoin (BTC) · …"). Only a segment that already names the asset exactly is
 * titled with it: metadata and page may see the segment percent-decoded a
 * different number of times (/assets/BTC%2520 is "BTC%20" here but "BTC%2520"
 * in the page, which shows not found), and a segment the page redirects never
 * shows this title.
 */
export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const symbol = exactSymbol((await params).symbol);
  return {
    title: symbol ? `${symbol} · Market Intelligence` : "Asset not found · Market Intelligence",
  };
}

export default async function Page({ params }: Props) {
  const { symbol } = await params;
  const canonical = canonicalSymbol(symbol);
  // One URL per asset: /assets/btc and /assets/BTC%20 go to /assets/BTC.
  if (isAssetSymbol(canonical) && canonical !== decodeSegment(symbol))
    redirect(`/assets/${encodeURIComponent(canonical)}`);
  return <AssetView symbol={canonical} />;
}
