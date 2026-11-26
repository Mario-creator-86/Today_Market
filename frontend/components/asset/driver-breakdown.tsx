import type { Decision } from "@/lib/api";
import { cn } from "@/lib/utils";
import { EmptyState } from "@/components/shared/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { driverModel } from "./drivers";
import { signed, signTone, toneText } from "./signal";

function Bar({ value, max }: { value: number; max: number }) {
  const width = `${Math.min(50, (Math.abs(value) / max) * 50)}%`;
  return (
    <div
      className="relative h-2 w-full min-w-[48px] rounded-sm bg-muted sm:min-w-[80px]"
      aria-hidden="true"
    >
      <span className="absolute inset-y-0 left-1/2 w-px bg-border" />
      <span
        className={cn(
          "absolute inset-y-0 rounded-sm",
          value >= 0
            ? "left-1/2 bg-positive/70"
            : "right-1/2 bg-destructive/70",
        )}
        style={{ width }}
      />
    </div>
  );
}

/**
 * WHY IS X MOVING: signed contributions per driver and the net score.
 * Missing components are labelled "Missing" and never drawn as zero.
 */
export function DriverBreakdown({
  decision,
  symbol,
}: {
  decision: Decision | null;
  symbol: string;
}) {
  const model = decision ? driverModel(decision) : null;
  if (!model)
    return (
      <EmptyState
        title="No driver breakdown yet"
        detail={`Contributions appear once ${symbol} has a scored decision with measured components.`}
      />
    );
  const rows = [...model.rows].sort((a, b) =>
    a.value === null
      ? 1
      : b.value === null
        ? -1
        : Math.abs(b.value) - Math.abs(a.value),
  );
  const max = Math.max(
    1,
    ...rows.map((r) => Math.abs(r.value ?? 0)),
    Math.abs(model.net ?? 0),
  );
  return (
    <div className="driver-breakdown">
      <Table aria-label={`Why is ${symbol} moving`} label={`${symbol} drivers`}>
        <TableHeader>
          <TableRow>
            <TableHead>Driver</TableHead>
            <TableHead className="w-[28%] px-2 sm:w-[40%] sm:px-[18px]">
              <span className="sr-only">Direction</span>
            </TableHead>
            <TableHead className="text-right">Contribution</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.name} data-missing={r.value === null || undefined}>
              <TableCell>
                <span className="font-medium">{r.name}</span>
                {r.basis && (
                  <span className="block text-[11px] text-muted-foreground">
                    {r.basis}
                  </span>
                )}
              </TableCell>
              <TableCell className="min-w-0 px-2 sm:px-[18px]">
                {r.value !== null && <Bar value={r.value} max={max} />}
              </TableCell>
              <TableCell
                className={cn(
                  "text-right font-mono tabular-nums",
                  r.value === null
                    ? "text-muted-foreground"
                    : toneText[signTone(r.value)],
                )}
              >
                {r.value === null ? "Missing" : signed(r.value)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
        <TableFooter>
          <TableRow>
            <TableCell className="font-semibold">Net</TableCell>
            <TableCell className="min-w-0 px-2 sm:px-[18px]">
              {model.net !== null && <Bar value={model.net} max={max} />}
            </TableCell>
            <TableCell
              className={cn(
                "text-right font-mono text-sm font-semibold tabular-nums",
                toneText[signTone(model.net)],
              )}
            >
              {model.net === null ? "Unavailable" : signed(model.net)}
            </TableCell>
          </TableRow>
        </TableFooter>
      </Table>
      <p className="px-5 py-3 text-[11px] text-muted-foreground">
        {model.basis}. Missing components add nothing to the score and are not
        measured zeros.
        {model.unmeasured.length > 0 && (
          <> Not measured: {model.unmeasured.join(", ")}.</>
        )}
      </p>
    </div>
  );
}
