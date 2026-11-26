import type { Asset } from "@/lib/api";
import { EmptyState } from "@/components/shared/empty-state";
import { levelRows, obj, scopeText } from "./levels";

/** SUPPORT / RESISTANCE and the other computed levels. */
export function KeyLevels({ asset }: { asset: Asset }) {
  const levels = obj(asset.technical_levels);
  if (!levels || !Object.keys(levels).length)
    return (
      <EmptyState
        title="Levels not established"
        detail="Support, resistance and other levels need enough recent price history from a live provider."
      />
    );
  const rows = levelRows(levels, asset.quote?.price, asset.asset_class);
  const scope = scopeText(levels);
  return (
    <>
      <dl className="detail-grid key-levels">
        {rows.map((r) => (
          <div key={r.label}>
            <dt>{r.label}</dt>
            <dd>
              <span className="font-mono tabular-nums">{r.value}</span>
              {r.note && (
                <span className="block text-[11px] text-muted-foreground">
                  {r.note}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      {scope && (
        <p className="px-5 pb-4 text-[11px] text-muted-foreground">{scope}</p>
      )}
    </>
  );
}
