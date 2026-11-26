"use client";
import { useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { adminHeaders, api, labelText, RecordData } from "@/lib/api";
import { text } from "@/lib/format";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { AiModel } from "./types";

const SLOTS = ["primary", "backup", "second_opinion"] as const;
type Slot = (typeof SLOTS)[number];
// Radix Select items cannot have an empty value: this one stands for "no model" in the optional slots.
const NONE = "__none__";

function assigned(value: unknown): string {
  return typeof value === "string" && value !== "Unavailable" ? value : "";
}

/** Normalise /models rows (objects or bare names) into {name, available}. */
export function modelOptions(models: unknown[]): { name: string; available: boolean }[] {
  const seen = new Map<string, boolean>();
  for (const m of models) {
    if (typeof m === "string") {
      if (m) seen.set(m, true);
      continue;
    }
    const row = (m ?? {}) as AiModel & RecordData;
    const name = text(row.name ?? row.id);
    if (!name || name === "Unavailable") continue;
    const available =
      row.availability !== false && String(row.status ?? "").toUpperCase() !== "UNAVAILABLE";
    seen.set(name, (seen.get(name) ?? false) || available);
  }
  return Array.from(seen, ([name, available]) => ({ name, available }));
}

/**
 * Assign discovered Ollama models to one AI role. Only available discovered
 * models can be chosen; unavailable ones are listed but disabled, and an
 * existing assignment that is no longer discovered stays visible but cannot
 * be saved.
 */
export function RoleForm({
  role,
  models,
  token,
}: {
  role: RecordData;
  models: unknown[];
  token: string;
}) {
  const id = useId();
  const cache = useQueryClient();
  const name = text(role.role ?? role.name);
  // The stored assignment; Save is offered only once the form differs from it.
  const [saved, setSaved] = useState<Record<Slot, string>>(() => ({
    primary: assigned(role.primary),
    backup: assigned(role.backup),
    second_opinion: assigned(role.second_opinion),
  }));
  const [values, setValues] = useState<Record<Slot, string>>(saved);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const options = modelOptions(models);
  const available = options.filter((o) => o.available).map((o) => o.name);
  const known = options.map((o) => o.name);
  const invalid = SLOTS.some((s) => values[s] !== "" && !available.includes(values[s]));
  const dirty = SLOTS.some((s) => values[s] !== saved[s]);
  const inherited = typeof role.inherited_from === "string" && role.inherited_from ? role.inherited_from : "";
  async function save() {
    if (!dirty) return;
    setBusy(true);
    try {
      await api(`/admin/roles/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: adminHeaders(token),
        body: JSON.stringify({
          primary: values.primary || null,
          backup: values.backup || null,
          second_opinion: values.second_opinion || null,
        }),
      });
      setSaved(values);
      setStatus("Assignment saved.");
      void cache.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="role-form"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <h3>{labelText(name)}</h3>
      {inherited && (
        // The backend copies the parent's primary and backup once, at startup (interpretation.ensure_role).
        <p className="mb-2 text-xs text-muted-foreground">
          Copied from {labelText(inherited)}&apos;s assignment when this role was first set up; later changes to{" "}
          {labelText(inherited)} do not carry over. Saving replaces it with this role&apos;s own assignment.
        </p>
      )}
      <div className="role-controls">
        {SLOTS.map((slot) => {
          const current = values[slot];
          const stale = current !== "" && !known.includes(current);
          return (
            <div className="field" key={slot}>
              <label htmlFor={`${id}-${slot}`}>{labelText(slot)}</label>
              <Select
                value={current}
                onValueChange={(v) => {
                  setValues({ ...values, [slot]: v === NONE ? "" : v });
                  setStatus("");
                }}
              >
                <SelectTrigger id={`${id}-${slot}`}>
                  <SelectValue placeholder={slot === "primary" ? "Select model" : "None"} />
                </SelectTrigger>
                <SelectContent>
                  {/* Backup and second opinion are optional and can be cleared. */}
                  {slot !== "primary" && <SelectItem value={NONE}>None</SelectItem>}
                  {stale && (
                    <SelectItem value={current} disabled>
                      {current} (not discovered)
                    </SelectItem>
                  )}
                  {options.map((o) => (
                    <SelectItem key={o.name} value={o.name} disabled={!o.available}>
                      {o.available ? o.name : `${o.name} (unavailable)`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          );
        })}
        <Button
          type="submit"
          aria-label={`Save assignment: ${labelText(name)}`}
          disabled={!token || !dirty || !available.includes(values.primary) || invalid || busy}
        >
          {busy ? "Saving…" : "Save assignment"}
        </Button>
      </div>
      <p role="status">
        {status ||
          (invalid
            ? "An assigned model is unavailable; choose an available model before saving."
            : "")}
      </p>
    </form>
  );
}
