"use client";
import { useState } from "react";
import Link from "next/link";
import { Info } from "lucide-react";
import { dataChip } from "@/lib/market-data";
import { cn } from "@/lib/utils";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { SearchToolbar } from "@/components/shared/search-toolbar";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  asOfText,
  fedRateText,
  fxAsset,
  fxDifferentialText,
  fxKey,
  fxMeetings,
  fxSearchText,
  isBasket,
  matchesText,
  meetingView,
  otherRateText,
  pairLabel,
  policyIncomplete,
  quoteAsOfLine,
  sortFx,
  yieldDiff,
} from "./page-logic";
import { Column, DataTable, fmt, tone } from "./page-utils";
import type { FxRow } from "./types";
import { useSectionData } from "./use-section-data";

/** Price with its provenance: DAILY CLOSE (ECB fix), DERIVED (USD index replica), STALE, … and the as-of line naming the source once. */
function PriceCell({ row }: { row: FxRow }) {
  const q = row.quote;
  const chip = dataChip(fxAsset(row));
  return (
    <span className="flex flex-col items-end gap-1 max-md:items-start">
      <span className="flex items-center gap-2">
        <span>{fmt(row.price ?? q?.price, { digits: 5 })}</span>
        <Badge variant={chip.tone} className={cn(chip.prominent && "font-extrabold")} title={chip.detail}>
          {chip.label}
        </Badge>
      </span>
      {q && (
        <small className="max-w-[16rem] whitespace-normal text-left text-[11px] text-muted-foreground">
          {quoteAsOfLine(q)}
        </small>
      )}
    </span>
  );
}

function MeetingCell({ row }: { row: FxRow }) {
  const view = meetingView(fxMeetings(row));
  if (!view.lines.length && !view.notes.length)
    return <span className="text-muted-foreground">No rate decision in the calendar</span>;
  return (
    <span className="flex flex-col gap-0.5">
      {view.lines.map((line) => (
        <span key={line}>{line}</span>
      ))}
      {view.notes.map((note) => (
        <small key={note} className="text-muted-foreground">
          {note}
        </small>
      ))}
      {view.lines.length > 0 && !view.priced && (
        <small className="text-muted-foreground">Market pricing unavailable</small>
      )}
    </span>
  );
}

/** A differential, or "Not applicable" for a basket (DXY has no single counterpart bank), with the backend's reason on hover. */
function DifferentialCell({ row, value }: { row: FxRow; value: unknown }) {
  const basket = isBasket(row);
  return (
    <span className={cn(basket && "text-muted-foreground")} title={basket ? (row.relative_basis ?? undefined) : undefined}>
      {fxDifferentialText(row, value)}
    </span>
  );
}

const COLUMNS: Column<FxRow>[] = [
  {
    key: "pair",
    label: "Pair",
    render: (r) => (
      <Link className="source-link font-semibold" href={`/assets/${encodeURIComponent(fxKey(r))}`}>
        {pairLabel(fxKey(r))}
      </Link>
    ),
  },
  {
    key: "price",
    label: "Price",
    numeric: true,
    className: "min-w-[150px]",
    render: (r) => <PriceCell row={r} />,
  },
  {
    key: "bias",
    label: "Bias",
    render: (r) =>
      r.bias ? <StatusBadge value={r.bias} variant={tone(r.bias)} /> : "Unavailable",
  },
  {
    key: "rates",
    label: "Policy rates",
    numeric: true,
    render: (r) => (
      <span className="flex flex-col items-end gap-0.5 max-md:items-start">
        <span title={r.fed_rate_basis ?? undefined}>Fed {fedRateText(r)}</span>
        <span
          className={cn(isBasket(r) && "text-muted-foreground")}
          title={(isBasket(r) ? r.relative_basis : r.other_rate_basis) ?? undefined}
        >
          {otherRateText(r)}
        </span>
      </span>
    ),
  },
  {
    key: "policy",
    label: "Policy differential",
    numeric: true,
    render: (r) => <DifferentialCell row={r} value={r.policy_differential} />,
  },
  {
    key: "y2",
    label: "2Y yield differential",
    numeric: true,
    render: (r) => <DifferentialCell row={r} value={yieldDiff(r)} />,
  },
  {
    key: "meeting",
    label: "Next rate decision",
    className: "min-w-[170px]",
    render: (r) => <MeetingCell row={r} />,
  },
];

/** Confirmation, invalidation and the backend's relative-policy verdict: full-width under the pair. */
function Detail({ row }: { row: FxRow }) {
  const reason = row.relative_basis ?? (policyIncomplete(row) ? row.reason : null);
  return (
    <dl className="m-0 grid gap-x-6 gap-y-1 text-[11px] md:grid-cols-2">
      <div>
        <dt className="inline font-semibold text-foreground">Confirmation: </dt>
        <dd className="inline">{row.confirmation || "Unavailable"}</dd>
      </div>
      <div>
        <dt className="inline font-semibold text-foreground">Invalidation: </dt>
        <dd className="inline">{row.invalidation || "Unavailable"}</dd>
      </div>
      {reason && (
        <div className="md:col-span-2">
          <dt className="inline font-semibold text-foreground">Relative policy: </dt>
          <dd className="inline">{reason}</dd>
        </div>
      )}
    </dl>
  );
}

/** Foreign exchange (/fx): relative policy table (§30). */
export function FxPage() {
  const [search, setSearch] = useState("");
  const q = useSectionData<FxRow[]>("fx");
  const ready = q.data !== undefined;
  const rows = sortFx(Array.isArray(q.data) ? q.data : []);
  const visible = rows.filter((r) => matchesText(fxSearchText(r), search));
  // DXY is a basket: it never has a single counterpart bank, so it is not counted as missing policy data.
  const pairs = rows.filter((r) => !isBasket(r));
  const incomplete = pairs.filter(policyIncomplete);
  const asOf = asOfText(rows.find((r) => r.as_of)?.as_of);
  return (
    <>
      <PageHeading
        title="Foreign exchange"
        description="Relative policy, rate differentials and market confirmation. Differentials are base minus quote central bank, in percentage points."
      />
      <QueryState loading={q.isLoading} error={q.error} onRetry={() => void q.refetch()} />
      {incomplete.length > 0 && (
        <Alert variant="info">
          <Info size={16} aria-hidden="true" />
          {incomplete.length === pairs.length
            ? "Relative policy is incomplete for every pair (both central banks' rates, 2Y yields and rate expectations are required), so every bias stays WAIT until relative policy data is verified."
            : `Relative policy is incomplete for ${incomplete.length} of ${pairs.length} pairs (a central-bank rate, 2Y yield or rate expectation is missing), so their bias stays WAIT. The reason is shown under each pair.`}
        </Alert>
      )}
      {ready && (
        <>
          <SearchToolbar search={search} onSearch={setSearch} count={visible.length} />
          <Card asChild className="panel">
            <section aria-label="Relative policy by pair">
              <DataTable
                rows={visible}
                columns={COLUMNS}
                rowKey={(r, i) => fxKey(r) || String(i)}
                caption="Relative policy by currency pair"
                dense
                detail={(r) => <Detail row={r} />}
                empty={
                  rows.length ? (
                    <EmptyState title="No matching pairs" detail="No pair matches the search." />
                  ) : (
                    <EmptyState
                      title="No FX data"
                      detail="Pairs appear when the market data provider delivers FX quotes."
                    />
                  )
                }
              />
              {asOf && (
                <p className="border-t border-border px-[18px] py-3 text-xs text-muted-foreground">
                  Policy inputs as of {asOf}
                </p>
              )}
            </section>
          </Card>
        </>
      )}
    </>
  );
}
