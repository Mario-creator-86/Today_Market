import { labelCase, text } from "@/lib/format";

function isScalar(value: unknown): boolean {
  return value === null || value === undefined || typeof value !== "object";
}

/** One value as readable text: lists as bullets, nested records as nested lists, never raw JSON. */
function DetailValue({ value }: { value: unknown }) {
  if (typeof value === "boolean") return <>{value ? "Yes" : "No"}</>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-muted-foreground">None</span>;
    return (
      <ul className="m-0 list-disc space-y-1 pl-4">
        {value.map((item, i) => (
          <li key={i}>{isScalar(item) ? text(item) : <DetailValue value={item} />}</li>
        ))}
      </ul>
    );
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value);
    if (!entries.length) return <span className="text-muted-foreground">None</span>;
    return (
      <dl className="m-0 grid gap-1">
        {entries.map(([key, item]) => (
          <div key={key}>
            <dt className="inline text-muted-foreground">{labelCase(key)}: </dt>
            <dd className="inline">
              <DetailValue value={item} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <>{text(value)}</>;
}

/** Key/value description list for configuration or interpretation objects. */
export function DetailList({ data }: { data: object }) {
  return (
    <dl className="detail-grid">
      {Object.entries(data).map(([key, value]) => (
        <div key={key}>
          <dt>{labelCase(key)}</dt>
          <dd>
            <DetailValue value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
