"use client";
import { EmptyState } from "@/components/shared/empty-state";
import { AlertsPage } from "./alerts";
import { CalendarPage } from "./calendar";
import { EventsPage } from "./events";
import { FxPage } from "./fx";
import { PerformancePage } from "./performance";
import { ProvidersPage } from "./provider-health";
import { SettingsPage } from "./settings";
import { SocialPage } from "./social";
import { SourcesPage } from "./sources";

const PAGES: Record<string, () => React.ReactNode> = {
  events: EventsPage,
  calendar: CalendarPage,
  fx: FxPage,
  sources: SourcesPage,
  settings: SettingsPage,
  performance: PerformancePage,
  providers: ProvidersPage,
  alerts: AlertsPage,
  social: SocialPage,
};

/** Dispatcher for app/[section]/page.tsx. Unknown sections render a not-found state. */
export function SectionView({ section }: { section: string }) {
  const Page = Object.hasOwn(PAGES, section) ? PAGES[section] : undefined;
  if (!Page)
    return (
      <EmptyState
        title="Page not found"
        detail="Choose a page from the workspace navigation."
      />
    );
  return <Page />;
}
