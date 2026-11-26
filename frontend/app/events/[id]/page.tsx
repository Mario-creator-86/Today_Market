import type { Metadata } from "next";
import { EventView } from "@/components/event/event-view";

// The headline replaces this once the event loads (EventView sets the document title).
export const metadata: Metadata = {
  title: "Event evidence · Market Intelligence",
};

export default async function Page({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <EventView id={id} />;
}
