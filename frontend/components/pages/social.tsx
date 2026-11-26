"use client";
import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { MessageSquareWarning } from "lucide-react";
import { api, localTime, safeUrl } from "@/lib/api";
import { humanize, matchesSearch } from "@/lib/format";
import { Alert } from "@/components/ui/alert";
import type { BadgeVariant } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/native-select";
import { EmptyState } from "@/components/shared/empty-state";
import { PageHeading } from "@/components/shared/page-heading";
import { QueryState } from "@/components/shared/query-state";
import { SearchToolbar } from "@/components/shared/search-toolbar";
import { StatusBadge } from "@/components/shared/status-badge";
import { isNotFound } from "./page-utils";
import type { SocialPost } from "./types";

/** Display statuses for X posts (§9), in escalation order. */
export const SOCIAL_STATUSES = [
  "UNVERIFIED",
  "OFFICIAL STATEMENT",
  "CORROBORATED",
  "CONFIRMED",
  "CONTRADICTED",
] as const;

const TONE: Record<string, BadgeVariant> = {
  UNVERIFIED: "neutral",
  "OFFICIAL STATEMENT": "info",
  CORROBORATED: "info",
  CONFIRMED: "positive",
  CONTRADICTED: "negative",
};

/** Normalise a backend status to a display status; unknown values stay UNVERIFIED. */
export function socialStatus(value: unknown): string {
  const label = typeof value === "string" ? value.toUpperCase().replaceAll("_", " ").trim() : "";
  return (SOCIAL_STATUSES as readonly string[]).includes(label) ? label : "UNVERIFIED";
}

/** GET /api/social row; `stub` marks a synthetic development post (PROVIDER_MODE=stub), never a real one. */
type Post = SocialPost;

function author(p: SocialPost): string {
  const handle = p.author_username ?? p.author;
  return [p.author_name, handle ? `@${String(handle).replace(/^@/, "")}` : null].filter(Boolean).join(" ") || "Unknown account";
}

/** Text a post shows, for search. */
function postSearchFields(p: Post): unknown[] {
  return [p.text, author(p), socialStatus(p.verification_status)];
}

function PostItem({ post }: { post: Post }) {
  const status = socialStatus(post.verification_status);
  // A development stub has no real post behind it: no "Open on X" link, however its URL looks.
  const href = post.stub
    ? undefined
    : (safeUrl(post.url) ?? (/^\d+$/.test(post.id) ? `https://x.com/i/status/${post.id}` : undefined));
  return (
    <article className="flex flex-col gap-2 border-t border-border px-[18px] py-4 first:border-t-0">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <StatusBadge value={status} variant={TONE[status]} />
        {post.stub && <StatusBadge value="DEVELOPMENT STUB" variant="outline" />}
        <strong className="text-sm">{author(post)}</strong>
        <time className="text-xs text-muted-foreground" dateTime={post.created_at ?? undefined}>
          {localTime(post.created_at)}
        </time>
      </header>
      <p className="text-sm wrap-anywhere">{post.text || "Post text unavailable"}</p>
      <div className="flex flex-wrap gap-4 text-xs">
        {href && (
          <a className="source-link" href={href} target="_blank" rel="noopener noreferrer">
            Open on X
          </a>
        )}
        {post.event_id && (
          <Link className="source-link" href={`/events/${encodeURIComponent(post.event_id)}`}>
            Linked event
          </Link>
        )}
      </div>
    </article>
  );
}

/** X / social feed (/social). Requires GET /api/social. */
export function SocialPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("ALL");
  const q = useQuery({
    queryKey: ["social"],
    queryFn: () => api<Post[]>("/social"),
    retry: (count, error) => !isNotFound(error) && count < 2,
  });
  const missing = isNotFound(q.error);
  // Nothing to count or to call empty until the feed has answered: QueryState reports loading and errors.
  const loaded = Array.isArray(q.data);
  const list = (Array.isArray(q.data) ? q.data : [])
    .filter((p) => status === "ALL" || socialStatus(p.verification_status) === status)
    .filter((p) => matchesSearch(p, search, postSearchFields))
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  return (
    <>
      <PageHeading
        title="X / social feed"
        description="Discovery only. A post never establishes a confirmed fact until independent or official sources verify it."
      />
      <Alert variant="info">
        <MessageSquareWarning size={16} aria-hidden="true" />
        Statuses: UNVERIFIED (unknown account), OFFICIAL STATEMENT (registered official account), CORROBORATED
        (at least one trusted source partially supports it), CONFIRMED, CONTRADICTED.
      </Alert>
      {!missing && <QueryState loading={q.isLoading} error={q.error} />}
      <SearchToolbar search={search} onSearch={setSearch} count={loaded ? list.length : null}>
        <NativeSelect aria-label="Social status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="ALL">All statuses</option>
          {SOCIAL_STATUSES.map((s) => (
            <option key={s} value={s}>
              {humanize(s)}
            </option>
          ))}
        </NativeSelect>
      </SearchToolbar>
      {(missing || loaded) && (
        <Card asChild className="panel">
          <section aria-label="Social posts">
            {missing ? (
              <EmptyState
                title="Social feed not available"
                detail="The backend does not provide the X feed yet. Posts will appear here once it does."
              />
            ) : list.length ? (
              list.map((p) => <PostItem key={p.id} post={p} />)
            ) : (
              <EmptyState title="No posts" detail="No X posts match the current filters, or the X provider is not configured." />
            )}
          </section>
        </Card>
      )}
    </>
  );
}
