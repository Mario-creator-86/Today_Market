"use client";
import { Bell, BellOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useBrowserNotifications } from "@/components/dashboard/notifications";

/**
 * Browser-notification opt-in (§40). Permission is requested only from the
 * click handler, and the opt-in is the same per-viewer preference the shell's
 * notifier reads, so enabling it here or on the dashboard behaves identically.
 */
export function NotificationControl() {
  const { permission, enabled, enable, disable } = useBrowserNotifications();
  if (permission === "unsupported")
    return (
      <span className="text-[11px] text-muted-foreground">
        Browser notifications unsupported
      </span>
    );
  if (permission === "denied")
    return (
      <span className="text-[11px] text-muted-foreground">
        Notifications blocked in browser settings
      </span>
    );
  return enabled ? (
    <Button variant="ghost" size="sm" onClick={disable}>
      <BellOff size={14} aria-hidden="true" />
      Disable browser notifications
    </Button>
  ) : (
    <Button variant="outline" size="sm" onClick={() => void enable()}>
      <Bell size={14} aria-hidden="true" />
      Enable browser notifications
    </Button>
  );
}
