"use client";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert } from "lucide-react";
import { api, isNotFound, localTime, MarketEvent, number } from "@/lib/api";
import {
  clampText,
  eventHeadline,
  importanceBasisText,
  inactivePathsNote,
  publishedText,
  reactionTiming,
  ruleTopic,
  triggerBasisText,
  type PathDetail,
} from "@/lib/events";
import { labelCase } from "@/lib/format";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { Panel } from "@/components/shared/panel";
import { QueryState } from "@/components/shared/query-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { isNum } from "@/components/asset/signal";
import { AssetSymbols, useTrackedSymbols } from "./asset-link";
import { CausalChain } from "./causal-chain";
import { factConfidenceLabel, reactionIndex, sortHorizons } from "./event-data";
import {
  Contradictions,
  evidenceIndex,
  SourceList,
  VerificationSummary,
  WhatHappened,
} from "./event-sections";
import { InterpretationView } from "./interpretation";
import { MarketConfirmation, ReactionTable } from "./reactions";

/** Poll interval of an open event (new evidence, reactions and verification changes). */
const REFRESH_MS = 30_000;
const APP_TITLE = "Market Intelligence";

/** The later of two ISO timestamps: an update is never shown before the first sighting. */
function later(a: string | undefined, b: string | undefined): string | undefined {
  const ta = a ? Date.parse(a) : NaN;
  const tb = b ? Date.parse(b) : NaN;
  if (!Number.isFinite(ta)) return b;
  if (!Number.isFinite(tb)) return a;
  return ta >= tb ? a : b;
}

function FactPanel({ event: e }: { event: MarketEvent }) {
  const tracked = useTrackedSymbols();
  const assets = e.affected_assets?.length
    ? e.affected_assets
    : (e.assets ?? []);
  const topic = ruleTopic(e);
  const basis = importanceBasisText(e.importance_basis);
  const triggerBasis = triggerBasisText(e.trigger_basis);
  // Every causal path faded or suppressed: the listed assets are no longer expected to move.
  const noActivePath = inactivePathsNote(e.causal_paths as PathDetail[] | undefined);
  return (
    <Card asChild className="fact-panel p-[26px]">
      <section aria-label="Fact">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge value={e.verification_status} />
          {e.event_type && (
            <StatusBadge value={e.event_type} variant="outline" />
          )}
          {topic && <StatusBadge value={topic} variant="outline" />}
        </div>
        <h2>{eventHeadline(e)}</h2>
        <p>
          {e.event_time && <>Event time {publishedText(e)} · </>}
          First seen {localTime(e.first_seen_utc)} · Updated{" "}
          {localTime(later(e.last_updated_utc, e.first_seen_utc))} (Bucharest)
        </p>
        <p className="mt-1">
          Affected assets:{" "}
          {assets.length ? (
            <AssetSymbols symbols={assets} tracked={tracked} />
          ) : (
            "not established"
          )}
          {assets.length > 0 && noActivePath && (
            <span className="text-muted-foreground"> ({noActivePath})</span>
          )}
          {isNum(e.importance) && (
            <>
              {` · Importance ${number(e.importance * 100, 0)}/100`}
              {basis && (
                <span title={e.importance_basis ?? undefined}> ({basis})</span>
              )}
            </>
          )}
          {e.trigger && ` · Trigger ${labelCase(e.trigger)}`}
          {e.trigger && triggerBasis && ` (${triggerBasis})`}
        </p>
        <span>Fact confidence: {factConfidenceLabel(e)}</span>
      </section>
    </Card>
  );
}

function NotFound() {
  return (
    <Card asChild className="panel">
      <section aria-label="Event not found">
        <EmptyState
          title="Event not found"
          detail="It may have been merged into another event or removed. The events list shows every stored event."
        />
        <p className="px-5 pb-5 text-center text-sm">
          <Link href="/events" className="source-link">
            Open the events list
          </Link>
        </p>
      </section>
    </Card>
  );
}

/** Event detail page (/events/[id]). */
export function EventView({ id }: { id: string }) {
  const q = useQuery({
    queryKey: ["event", id],
    queryFn: () => api<MarketEvent>(`/events/${encodeURIComponent(id)}`),
    // A 404 is an answer, not an outage: no retry and no polling of a missing event.
    retry: (count, error) => !isNotFound(error) && count < 1,
    refetchInterval: (query) =>
      isNotFound(query.state.error) ? false : REFRESH_MS,
  });
  const missing = isNotFound(q.error);
  const e = missing ? undefined : q.data;
  const headline = e ? eventHeadline(e) : "";
  useEffect(() => {
    if (missing) document.title = `Event not found · ${APP_TITLE}`;
    else if (headline)
      document.title = `${clampText(headline, 70)} · Event evidence · ${APP_TITLE}`;
  }, [missing, headline]);
  const reactions = useMemo(() => reactionIndex(e?.reactions), [e?.reactions]);
  const numbers = useMemo(() => (e ? evidenceIndex(e).numbers : new Map<string, number>()), [e]);
  const horizons = useMemo(
    () =>
      sortHorizons([
        ...(e?.reactions ?? []).map((r) => r.horizon),
        ...(e?.baskets ?? []).map((b) => b.horizon),
      ]),
    [e?.reactions, e?.baskets],
  );
  const [picked, setPicked] = useState<string>();
  // Default to the longest window measured so far.
  const horizon =
    picked && horizons.includes(picked)
      ? picked
      : horizons[horizons.length - 1];
  const windowPicker = horizons.length > 1 && (
    <NativeSelect
      aria-label="Reaction window"
      value={horizon}
      onChange={(ev) => setPicked(ev.target.value)}
      className="py-[5px] text-xs"
    >
      {horizons.map((h) => (
        <option key={h} value={h}>
          {h}
        </option>
      ))}
    </NativeSelect>
  );
  return (
    <>
      <Link href="/events" className="back-link">
        Back to events
      </Link>
      <PageHeading
        title="Event evidence"
        description="What happened, how it was verified, and what the market did."
      />
      {missing ? (
        <NotFound />
      ) : (
        <QueryState loading={q.isLoading} error={q.error} />
      )}
      {e && (
        <>
          {e.injection_suspected && (
            <Alert variant="warning">
              <ShieldAlert size={16} aria-hidden="true" />
              <AlertTitle className="inline text-warning">
                Injection suspected
              </AlertTitle>
              <AlertDescription className="mt-1">
                A source for this event contains text that looks like
                instructions to automated systems. Treat AI-derived fields (what
                happened, interpretation) with caution and rely on the source
                documents below.
              </AlertDescription>
            </Alert>
          )}
          <FactPanel event={e} />
          <div className="dashboard-columns">
            <Panel title="What happened">
              <WhatHappened event={e} />
            </Panel>
            <Panel title="Verification status">
              <VerificationSummary event={e} />
            </Panel>
          </div>
          <div className="dashboard-columns">
            <Panel title="Sources">
              <SourceList event={e} />
            </Panel>
            <Panel title="Contradictions">
              <Contradictions event={e} />
            </Panel>
          </div>
          <div className="mt-6">
            <Panel title="Causal chain" action={windowPicker}>
              <CausalChain
                event={e}
                horizon={horizon}
                reactions={reactions}
                late={reactionTiming(e).late}
              />
            </Panel>
          </div>
          <div className="dashboard-columns">
            <Panel title="Expected vs actual reaction">
              <ReactionTable event={e} reactions={reactions} />
            </Panel>
            <Panel title="Market confirmation">
              <MarketConfirmation event={e} horizon={horizon} />
            </Panel>
          </div>
          <div className="mt-6">
            <Panel title="AI interpretation">
              <InterpretationView event={e} numbers={numbers} />
            </Panel>
          </div>
        </>
      )}
    </>
  );
}
