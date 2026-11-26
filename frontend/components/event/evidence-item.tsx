import { ArrowUpRight } from "lucide-react";
import { localTime, number, RecordData, safeUrl } from "@/lib/api";
import { sourceText } from "@/lib/events";
import { StatusBadge } from "@/components/shared/status-badge";
import { isNum } from "@/components/asset/signal";
import { directness, isContradicting, str, trustTier } from "./event-data";

/**
 * One evidence document: reference number, source name, trust (tier / weight
 * / official), direct or second-hand, stance, title and excerpt as plain
 * text (feed markup removed, times in Bucharest), safe link and both
 * timestamps. Other copies of the same address are counted, not listed
 * again (`copyNote` says why); a document from another publication date is
 * flagged (`otherDate`).
 */
export function EvidenceItem({
  source,
  number: ref,
  copyNote,
  otherDate,
}: {
  source: RecordData;
  /** 1-based reference number used by "[n]" citations; omitted when not numbered. */
  number?: number;
  /** Why this entry stands for several received copies (lib/events evidenceCopyNote); omitted for one copy. */
  copyNote?: string | null;
  /** The document was published on another date than the event (lib/events otherPublicationText). */
  otherDate?: string | null;
}) {
  const url = safeUrl(source.url);
  const name =
    str(source.source) ?? str(source.source_name) ?? str(source.source_id);
  const tier = trustTier(source);
  const direct = directness(source);
  const stance =
    source.stance ||
    source.relation ||
    source.verification_status ||
    "UNASSESSED";
  const title = sourceText(source.title);
  const body = sourceText(source.excerpt) || sourceText(source.summary);
  return (
    <div
      className="evidence scroll-mt-24"
      id={ref === undefined ? undefined : `evidence-${ref}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {ref !== undefined && (
          <span className="font-mono text-xs text-muted-foreground">[{ref}]</span>
        )}
        <strong className="mr-1 text-sm">{name ?? "Unnamed source"}</strong>
        <StatusBadge
          value={stance}
          variant={
            isContradicting(source)
              ? "negative"
              : String(stance).toLowerCase() === "supports"
                ? "positive"
                : undefined
          }
        />
        {source.official === true && (
          <StatusBadge value="OFFICIAL" variant="info" />
        )}
        {direct && <StatusBadge value={direct} variant="outline" />}
        {source.discovery_only === true && (
          <StatusBadge value="DISCOVERY ONLY" variant="outline" />
        )}
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        {tier ? `Trust tier ${tier}` : "Trust tier not provided"}
        {isNum(source.weight) && ` · Weight ${number(source.weight, 2)}`}
        {str(source.group) && ` · Independence group ${source.group}`}
        {copyNote && <span className="mt-1 block">{copyNote}</span>}
      </p>
      {otherDate && (
        <p role="note" className="text-xs text-warning">
          {otherDate}.
        </p>
      )}
      {title && title !== body && (
        <h3 className="mt-3 text-sm font-medium">{title}</h3>
      )}
      {body ? (
        <p className="line-clamp-4">{body}</p>
      ) : (
        <p className="text-xs text-muted-foreground">
          No excerpt captured; open the source for the full text.
        </p>
      )}
      {url ? (
        <a href={url} target="_blank" rel="noopener noreferrer">
          Read source <ArrowUpRight size={14} aria-hidden="true" />
        </a>
      ) : (
        <span>Source link unavailable</span>
      )}
      <small>
        Published {localTime(source.published_at)} · Ingested{" "}
        {localTime(source.ingested_at)} (Bucharest)
      </small>
    </div>
  );
}
