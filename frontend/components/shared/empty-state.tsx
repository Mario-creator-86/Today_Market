import type { LucideIcon } from "lucide-react";
import { ShieldCheck } from "lucide-react";

/** Placeholder for missing data. Never implies a measured zero. */
export function EmptyState({
  title = "No records yet",
  detail = "Data will appear when a configured provider delivers verified inputs.",
  icon: Icon = ShieldCheck,
}: {
  title?: string;
  detail?: string;
  icon?: LucideIcon;
}) {
  return (
    <div className="empty">
      <Icon size={27} aria-hidden="true" />
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}
