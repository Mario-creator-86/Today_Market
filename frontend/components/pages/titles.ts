/**
 * Browser titles for /[section] routes. Kept free of "use client" so the
 * server route can read it in generateMetadata.
 */
export const SECTION_TITLES: Record<string, string> = {
  events: "Event intelligence",
  calendar: "Market calendar",
  fx: "Foreign exchange",
  sources: "Sources & verification",
  settings: "Workspace settings",
  performance: "Historical performance",
  providers: "Provider health",
  alerts: "Alerts",
  social: "X / social feed",
};
