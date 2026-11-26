import { describe, expect, it } from "vitest";
import { scrollRegionLabel } from "./table";

describe("table scroll region name", () => {
  it("uses the explicit label (a DataTable caption)", () => {
    expect(scrollRegionLabel("Paper trades", "ignored")).toBe("Paper trades (scrolls sideways)");
  });
  it("falls back to the table's own aria-label, as the asset and event tables pass it", () => {
    expect(scrollRegionLabel(undefined, "Historical comparable BTC signals")).toBe(
      "Historical comparable BTC signals (scrolls sideways)",
    );
    expect(scrollRegionLabel("  ", "Reaction after the event")).toBe("Reaction after the event (scrolls sideways)");
  });
  it("names an unlabelled table generically", () => {
    expect(scrollRegionLabel()).toBe("Table (scrolls sideways)");
    expect(scrollRegionLabel(null, null)).toBe("Table (scrolls sideways)");
  });
});
