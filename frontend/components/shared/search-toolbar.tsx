import { Search } from "lucide-react";

/** Search box + optional filter controls + record count for list pages. */
export function SearchToolbar({
  search,
  onSearch,
  count,
  countLabel,
  children,
}: {
  search: string;
  onSearch: (value: string) => void;
  /**
   * Visible records; null or undefined hides the count (no data loaded, or the page states the count itself, e.g. in a
   * panel header: one counter per list, so a screen reader announces a change once).
   */
  count?: number | null;
  /** Replaces "N records", e.g. "18 events, showing 10". */
  countLabel?: string;
  /** Filter controls rendered after the search box. */
  children?: React.ReactNode;
}) {
  const shown = countLabel ?? (typeof count === "number" ? `${count} records` : null);
  return (
    // Wraps at every width: a search box and several selects never widen the page.
    <div className="toolbar flex-wrap">
      <label className="search">
        <Search size={16} aria-hidden="true" />
        <input
          aria-label="Search records"
          placeholder="Search records…"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
        />
      </label>
      {children}
      {shown && (
        <span className="muted" role="status">
          {shown}
        </span>
      )}
    </div>
  );
}
