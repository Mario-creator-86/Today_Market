"use client";
import { useEffect, useRef } from "react";

/** Which edges of a sideways scroller have more items behind them. */
function fadeOf(box: HTMLElement): "start" | "end" | "both" | "none" {
  const max = box.scrollWidth - box.clientWidth;
  const start = box.scrollLeft > 2;
  const end = max > 2 && box.scrollLeft < max - 2;
  return start && end ? "both" : start ? "start" : end ? "end" : "none";
}

/** Centre the item matching `selector` in the scroller (nothing to do when every item fits). */
function reveal(box: HTMLElement, selector: string) {
  const item = box.querySelector<HTMLElement>(selector);
  if (!item || box.scrollWidth <= box.clientWidth) return;
  const outer = box.getBoundingClientRect();
  const inner = item.getBoundingClientRect();
  box.scrollLeft += inner.left - outer.left - (outer.width - inner.width) / 2;
}

/**
 * A row that scrolls sideways (the phone navigation, a tab strip). `data-fade` on it names the edges with more items
 * behind them ("start", "end", "both" or "none") for the CSS edge fade, and the current item (`selector`) is kept in
 * view: centred on mount and whenever `key` changes, and, with `watch` (an attribute name), whenever that attribute
 * changes on an item inside the row, e.g. Radix's data-state when another tab is selected by click or keyboard.
 */
export function useEdgeFade<T extends HTMLElement>(selector: string, key?: unknown, watch?: string) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const box = ref.current;
    if (!box) return;
    const update = () => {
      box.dataset.fade = fadeOf(box);
    };
    update();
    box.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    // The row can change size without the window doing so (fonts arriving, a parent reflowing).
    const resized = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    resized?.observe(box);
    const selected =
      watch && typeof MutationObserver !== "undefined"
        ? new MutationObserver(() => {
            reveal(box, selector);
            update();
          })
        : null;
    if (watch) selected?.observe(box, { subtree: true, attributes: true, attributeFilter: [watch] });
    return () => {
      box.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      resized?.disconnect();
      selected?.disconnect();
    };
  }, [selector, watch]);
  // The current item starts in view, also on a direct load of a page far along the row.
  useEffect(() => {
    const box = ref.current;
    if (box) reveal(box, selector);
  }, [key, selector]);
  return ref;
}
