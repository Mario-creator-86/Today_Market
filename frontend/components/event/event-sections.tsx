import { MarketEvent } from "@/lib/api";
import {
  evidenceCopyNote,
  evidenceNumbers,
  evidenceSummary,
  groupEvidence,
  otherPublication,
  otherPublicationText,
  sourceText,
  type EvidenceGroup,
} from "@/lib/events";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusBadge } from "@/components/shared/status-badge";
import { AnnotatedText } from "./annotated-text";
import {
  directness,
  extraction,
  factConfidenceLabel,
  isContradicting,
  str,
  strings,
} from "./event-data";
import { EvidenceItem } from "./evidence-item";

/** Numbered evidence of an event (duplicate copies collapsed) and the id -> number map citations use. */
export function evidenceIndex(event: MarketEvent) {
  const groups = groupEvidence(event.evidence);
  return { groups, numbers: evidenceNumbers(groups) };
}

/** One numbered evidence card, with why it stands for several copies and whether it is from another date. */
function EvidenceCard({ event, group }: { event: MarketEvent; group: EvidenceGroup }) {
  const gap = otherPublication(group.item, event);
  return (
    <EvidenceItem
      source={group.item}
      number={group.number}
      copyNote={evidenceCopyNote(group)}
      otherDate={gap ? otherPublicationText(gap) : null}
    />
  );
}

function List({
  items,
  numbers,
}: {
  items: string[];
  numbers: Map<string, number>;
}) {
  return (
    <ul className="m-0 list-disc space-y-1 pl-4">
      {items.map((item, i) => (
        <li key={i}>
          <AnnotatedText text={item} numbers={numbers} />
        </li>
      ))}
    </ul>
  );
}

/** WHAT HAPPENED: extractor output (who/what/when/where/claims), or source text. */
export function WhatHappened({ event }: { event: MarketEvent }) {
  const { numbers } = evidenceIndex(event);
  const x = extraction(event);
  const who = strings(x?.who);
  const claims = strings(x?.direct_claims);
  const fresh = strings(x?.new_information);
  if (x && (str(x.what) || who.length || claims.length)) {
    const what = str(x.what);
    const when = str(x.when);
    const where = str(x.where);
    return (
      <dl className="detail-grid what-happened">
        {what && (
          <div>
            <dt>What</dt>
            <dd>
              <AnnotatedText text={what} numbers={numbers} />
            </dd>
          </div>
        )}
        {who.length > 0 && (
          <div>
            <dt>Who</dt>
            <dd>{who.join(", ")}</dd>
          </div>
        )}
        {when && (
          <div>
            <dt>When (as stated)</dt>
            <dd>
              <AnnotatedText text={when} numbers={numbers} />
            </dd>
          </div>
        )}
        {where && (
          <div>
            <dt>Where</dt>
            <dd>{where}</dd>
          </div>
        )}
        {claims.length > 0 && (
          <div>
            <dt>Direct claims</dt>
            <dd>
              <List items={claims} numbers={numbers} />
            </dd>
          </div>
        )}
        {fresh.length > 0 && (
          <div>
            <dt>New information</dt>
            <dd>
              <List items={fresh} numbers={numbers} />
            </dd>
          </div>
        )}
        <p className="col-span-full text-[11px] text-muted-foreground">
          Extracted from the source documents by the AI extractor; numbers in
          brackets point to the sources below. Verification status comes from
          the evidence, not the extractor.
        </p>
      </dl>
    );
  }
  const first = (event.evidence ?? []).find(
    (e) => sourceText(e.excerpt) || sourceText(e.title),
  );
  if (first)
    return (
      <div className="p-5 text-sm">
        <p className="mb-2 text-[11px] uppercase tracking-wide text-muted-foreground">
          Source text ({str(first.source) ?? "unnamed source"}) · not yet
          extracted
        </p>
        <p className="line-clamp-6">
          {sourceText(first.excerpt) || sourceText(first.title)}
        </p>
      </div>
    );
  return (
    <EmptyState
      title="No structured account yet"
      detail="The extractor has not processed this event and no source text is attached."
    />
  );
}

/** VERIFICATION STATUS: status, confidence and how the evidence supports it. */
export function VerificationSummary({ event }: { event: MarketEvent }) {
  const { groups: docs, numbers } = evidenceIndex(event);
  const evidence = docs.map((d) => d.item);
  // Same counts as /sources: duplicate copies of one address count once.
  const {
    duplicates,
    contradicting,
    independentGroups: groups,
  } = evidenceSummary(docs);
  const official = evidence.filter((e) => e.official === true).length;
  const otherDates = docs.filter((d) => otherPublication(d.item, event)).length;
  const direct = evidence.filter((e) => directness(e) === "Direct").length;
  const fc = event.fact_check;
  const verified = strings(fc?.verified_statements);
  const unverified = strings(fc?.unverified_statements);
  const explanation = str(fc?.explanation);
  return (
    <dl className="detail-grid verification">
      <div>
        <dt>Status</dt>
        <dd>
          <StatusBadge value={event.verification_status} />
        </dd>
      </div>
      <div>
        <dt>Fact confidence</dt>
        <dd>{factConfidenceLabel(event)}</dd>
      </div>
      <div>
        <dt>Evidence</dt>
        <dd>
          {evidence.length} document{evidence.length === 1 ? "" : "s"} from{" "}
          {groups} independent source group{groups === 1 ? "" : "s"}
          {duplicates > 0 &&
            ` (${duplicates} duplicate cop${duplicates === 1 ? "y" : "ies"} of the same address not counted)`}
          {otherDates > 0 && (
            <span className="mt-1 block text-warning">
              {otherDates === 1
                ? "1 document was published on another date: probably a separate release, not another report of this event."
                : `${otherDates} documents were published on other dates: probably separate releases, not other reports of this event.`}
            </span>
          )}
        </dd>
      </div>
      <div>
        <dt>Official / direct</dt>
        <dd>
          {official} official · {direct} direct
        </dd>
      </div>
      <div>
        <dt>Contradicting</dt>
        <dd className={contradicting ? "text-destructive" : undefined}>
          {contradicting} document{contradicting === 1 ? "" : "s"}
        </dd>
      </div>
      {verified.length > 0 && (
        <div>
          <dt>Verified statements</dt>
          <dd>
            <List items={verified} numbers={numbers} />
          </dd>
        </div>
      )}
      {unverified.length > 0 && (
        <div>
          <dt>Unverified statements</dt>
          <dd>
            <List items={unverified} numbers={numbers} />
          </dd>
        </div>
      )}
      {explanation && (
        <div>
          <dt>Explanation</dt>
          <dd>
            <AnnotatedText text={explanation} numbers={numbers} />
          </dd>
        </div>
      )}
    </dl>
  );
}

/** SOURCES: every evidence document except contradicting ones (listed separately). */
export function SourceList({ event }: { event: MarketEvent }) {
  const items = evidenceIndex(event).groups.filter(
    (g) => !isContradicting(g.item),
  );
  if (!items.length)
    return (
      <EmptyState
        title="No supporting sources attached"
        detail="Supporting documents appear here with their source, trust and timestamps."
      />
    );
  return (
    <>
      {items.map((g) => (
        <EvidenceCard key={g.number} event={event} group={g} />
      ))}
    </>
  );
}

/** CONTRADICTIONS: contradicting evidence plus fact-check contradictions. */
export function Contradictions({ event }: { event: MarketEvent }) {
  const { groups, numbers } = evidenceIndex(event);
  const items = groups.filter((g) => isContradicting(g.item));
  const notes = strings(event.fact_check?.contradictions);
  if (!items.length && !notes.length)
    return (
      <EmptyState
        title="No contradicting evidence found"
        detail="Contradicting reports would be listed here and lower the fact confidence."
      />
    );
  return (
    <>
      {notes.length > 0 && (
        <div className="border-b border-border p-5 text-sm">
          <List items={notes} numbers={numbers} />
        </div>
      )}
      {items.map((g) => (
        <EvidenceCard key={g.number} event={event} group={g} />
      ))}
    </>
  );
}
