"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Brief, briefLateText, localTime } from "@/lib/api";
import { readableLabel } from "@/lib/market-data";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { Panel } from "@/components/shared/panel";
import { QueryState } from "@/components/shared/query-state";

type BriefKind = "" | "MORNING";

/**
 * On-demand morning/intraday brief reader. A morning brief composed after its
 * scheduled window says so (its values are those of the later moment), and
 * the empty answer gives the service's own reason.
 */
export function MarketBrief() {
  const [kind, setKind] = useState<BriefKind>("");
  const brief = useQuery({
    queryKey: ["brief", kind],
    queryFn: () => api<Brief>(kind ? `/brief?kind=${kind}` : "/brief"),
    enabled: false,
  });
  const late = briefLateText(brief.data);
  const reason = typeof brief.data?.reason === "string" ? brief.data.reason.trim() : "";
  return (
    <Panel title="Market brief">
      <div className="brief">
        <p>
          A concise summary of the latest evidence and changes in market
          conditions.
        </p>
        <div className="brief-actions">
          <NativeSelect
            aria-label="Brief type"
            value={kind}
            onChange={(e) => setKind(e.target.value as BriefKind)}
          >
            <option value="">Latest update</option>
            <option value="MORNING">Morning brief</option>
          </NativeSelect>
          <Button onClick={() => brief.refetch()} disabled={brief.isFetching}>
            {brief.isFetching ? "Loading…" : "Read brief"}
          </Button>
        </div>
        <QueryState loading={false} error={brief.error} />
        {brief.data && !brief.data.generated_at && (
          <p className="muted">{reason ? `${readableLabel(reason)}.` : "No brief of this type has been generated yet."}</p>
        )}
        {brief.data?.generated_at && (
          <p className="muted">
            {brief.data.kind === "MORNING"
              ? "Morning brief"
              : "Intraday update"}{" "}
            · {localTime(brief.data.generated_at)}
            {late && ` · ${late}`}
          </p>
        )}
        {brief.data?.sections.map((s, i) => (
          <div key={i}>
            <h3>{s.title}</h3>
            <ul>
              {s.items.map((item, j) => (
                <li key={j}>{item}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Panel>
  );
}
