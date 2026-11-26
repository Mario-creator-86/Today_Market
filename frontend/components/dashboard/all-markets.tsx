import Link from "next/link";
import type { Asset } from "@/lib/api";
import { Panel } from "@/components/shared/panel";
import { marketGroups, symbolLabel } from "./view-model";

/**
 * Every tracked market, grouped by asset class, each linking to its asset
 * page: the strip and the setups show only a selection, so this is the one
 * place every asset page is reachable from.
 */
export function AllMarkets({ assets }: { assets: Asset[] }) {
  const groups = marketGroups(assets);
  if (!groups.length) return null;
  return (
    <div className="mt-6">
      <Panel title="All markets">
        <dl className="m-0 grid gap-3 px-[21px] py-4">
          {groups.map((g) => (
            <div key={g.key} className="grid gap-1.5 sm:grid-cols-[110px_minmax(0,1fr)] sm:items-baseline">
              <dt className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                {g.label}
              </dt>
              <dd className="m-0">
                <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" aria-label={`${g.label} markets`}>
                  {g.assets.map((a) => (
                    <li key={a.symbol}>
                      <Link
                        href={`/assets/${encodeURIComponent(a.symbol)}`}
                        title={a.name || undefined}
                        className="inline-flex min-h-7 items-center rounded-md border border-border px-2.5 text-xs font-semibold hover:bg-secondary/60"
                      >
                        {symbolLabel(a.symbol)}
                      </Link>
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          ))}
        </dl>
      </Panel>
    </div>
  );
}
