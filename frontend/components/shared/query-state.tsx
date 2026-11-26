"use client";
import { useState } from "react";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { describeError } from "@/lib/api";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

/**
 * Loading (role=status) and error (role=alert) notices for a TanStack query. The wording follows what happened:
 * a 404 is "Not found", another error status is "The service returned an error (HTTP N)", and only a request that
 * got no answer says "Unable to reach the data service". Failed loads are retried in the background; `onRetry`
 * adds a button to try at once.
 *
 * While a failed load is asked again the notice stays as it was (the Retry icon turns): TanStack clears `error` and
 * reports loading again while a query without data refetches, which would otherwise swap the notice for "Loading…"
 * on every background retry and make screen readers announce the outage again each time.
 */
export function QueryState({
  loading,
  error,
  loadingText = "Loading market intelligence…",
  onRetry,
  notFound,
}: {
  loading: boolean;
  error: Error | null;
  loadingText?: string;
  onRetry?: () => void;
  /** Where to go instead when the record does not exist, e.g. {href: "/events", label: "Back to events"}. */
  notFound?: { href: string; label: string };
}) {
  // The last failure, kept while the query is asked again; forgotten once a load succeeds.
  const [kept, setKept] = useState<Error | null>(null);
  if (error && error !== kept) setKept(error);
  else if (!error && !loading && kept) setKept(null);
  const shown = error ?? (loading ? kept : null);
  const retrying = loading && !error && shown !== null;
  // Keys: the error notice is a new element (announced as an alert), never the loading notice changing its role.
  if (!shown)
    return loading ? (
      <Alert key="loading" role="status">
        {loadingText}
      </Alert>
    ) : null;
  const view = describeError(shown);
  if (view.kind === "not_found")
    return (
      <Alert key="not-found" variant="warning">
        <strong className="mr-1">{view.title}</strong>
        {view.detail}
        {notFound && (
          <>
            {" "}
            <Link href={notFound.href} className="source-link">
              {notFound.label}
            </Link>
          </>
        )}
      </Alert>
    );
  return (
    <Alert key="error" variant="destructive" className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <span className="min-w-0 flex-1 basis-60 wrap-anywhere">
        <strong className="mr-1">{view.title}</strong>
        {view.detail}
      </span>
      {onRetry && (
        // The label stays the same while retrying (a changed label would be announced); the icon turns instead.
        <Button type="button" size="sm" variant="outline" onClick={onRetry} aria-busy={retrying || undefined}>
          <RefreshCw size={14} aria-hidden="true" className={retrying ? "spin" : undefined} />
          Retry now
        </Button>
      )}
    </Alert>
  );
}
