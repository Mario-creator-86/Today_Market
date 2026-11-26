import type { Metadata } from "next";
import Link from "next/link";
import { Compass } from "lucide-react";
import { PageHeading } from "@/components/shared/page-heading";

export const metadata: Metadata = {
  title: "Page not found · Market Intelligence",
};

/** 404 for unknown addresses, inside the workspace shell and its theme. */
export default function NotFound() {
  return (
    <>
      <PageHeading
        title="Page not found"
        description="There is no page at this address. It may have been moved, or the link is mistyped."
      />
      <div className="empty">
        <Compass size={27} aria-hidden="true" />
        <strong>Choose a page from the workspace navigation</strong>
        <p>
          <Link href="/" className="source-link">
            Back to the dashboard
          </Link>
        </p>
      </div>
    </>
  );
}
