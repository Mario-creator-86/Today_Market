import { annotate } from "@/lib/events";

/**
 * Extractor or interpreter text with ISO timestamps shown in Bucharest time
 * and cited evidence ids shown as numbered links to the evidence cards
 * ("[2]"), never as raw UUIDs.
 */
export function AnnotatedText({
  text,
  numbers,
}: {
  text: string;
  numbers: Map<string, number>;
}) {
  return (
    <>
      {annotate(text, numbers).map((part, i) =>
        part.kind === "text" ? (
          <span key={i}>{part.text}</span>
        ) : part.number === null ? (
          <sup key={i} className="text-muted-foreground" title="The cited document is not attached to this event">
            [source not listed]
          </sup>
        ) : (
          <sup key={i}>
            <a
              href={`#evidence-${part.number}`}
              className="source-link"
              aria-label={`Evidence ${part.number}`}
            >
              [{part.number}]
            </a>
          </sup>
        ),
      )}
    </>
  );
}

/** "Evidence [1] [3]" for a list of cited evidence ids; nothing when none are cited. */
export function EvidenceRefs({
  ids,
  numbers,
}: {
  ids: string[];
  numbers: Map<string, number>;
}) {
  if (!ids.length) return null;
  const cited = [...new Set(ids.map((id) => numbers.get(id) ?? numbers.get(id.toLowerCase()) ?? null))];
  return (
    <p className="mt-2 text-[11px] text-muted-foreground">
      Evidence{" "}
      {cited.map((n, i) =>
        n === null ? (
          <span key={`missing-${i}`}>[source not listed] </span>
        ) : (
          <a key={n} href={`#evidence-${n}`} className="source-link mr-1" aria-label={`Evidence ${n}`}>
            [{n}]
          </a>
        ),
      )}
    </p>
  );
}
