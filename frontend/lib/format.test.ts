import { describe, expect, it } from "vitest";
import { humanize, labelCase, matchesSearch, plainText, statusTone, text } from "./format";
import { localDay } from "./api";
import { cn } from "./utils";

describe("format helpers", () => {
  it("never renders missing values as zero", () => {
    expect(text(null)).toBe("Unavailable");
    expect(text(undefined)).toBe("Unavailable");
    expect(text(0)).toBe("0");
    expect(text({ a: 1 })).toBe('{"a":1}');
  });
  it("searches record values case-insensitively", () => {
    expect(matchesSearch({ name: "US CPI" }, "cpi")).toBe(true);
    expect(matchesSearch({ name: "US CPI" }, "nfp")).toBe(false);
    expect(matchesSearch({ name: "US CPI" }, "")).toBe(true);
    expect(matchesSearch({ name: "US CPI" }, "  cpi ")).toBe(true);
  });
  it("never matches keys or JSON punctuation", () => {
    expect(matchesSearch({ impact: 9, name: "CPI" }, "impact")).toBe(false);
    expect(matchesSearch({ id: "x1", name: "CPI" }, '"id"')).toBe(false);
    expect(matchesSearch({ nested: { label: "ECB reference rate" } }, "ecb")).toBe(true);
  });
  it("never matches ids or UUIDs without a field selector", () => {
    expect(matchesSearch({ id: "ecb2ec95", name: "US industrial production" }, "ecb")).toBe(false);
    expect(matchesSearch({ event_id: "ecb2ec95", name: "x" }, "ecb")).toBe(false);
    expect(matchesSearch({ evidence_ids: ["ecb2ec95"], name: "x" }, "ecb")).toBe(false);
    expect(
      matchesSearch({ ref: "ecb2ec95-6311-4f82-b115-2e15b5f5a7f9", name: "x" }, "ecb2ec95"),
    ).toBe(false);
    expect(matchesSearch({ id: "ecb2ec95", name: "ECB rate decision" }, "ecb")).toBe(true);
    // A page that names its fields decides what is shown, ids included.
    expect(matchesSearch({ id: "BTC" }, "btc", (r) => [r.id])).toBe(true);
  });
  it("searches only the shown fields when the page names them", () => {
    const row = { id: "ecb2ec95", name: "US industrial production (G.17)", provenance: "provider_retrieval" };
    const shown = (r: typeof row) => [r.name];
    expect(matchesSearch(row, "industrial", shown)).toBe(true);
    expect(matchesSearch(row, "ecb", shown)).toBe(false);
    expect(matchesSearch(row, "provider_retrieval", shown)).toBe(false);
  });
  it("gives one status one badge tone on every page", () => {
    expect(statusTone("PARTIALLY CONFIRMED")).toBe("info");
    expect(statusTone("PARTIALLY_CONFIRMED")).toBe("info");
    expect(statusTone("CORROBORATED")).toBe("info");
    expect(statusTone("OFFICIAL STATEMENT")).toBe("info");
    expect(statusTone("CONFIRMED")).toBe("positive");
    expect(statusTone("FALSE")).toBe("negative");
    expect(statusTone("AUTH ERROR")).toBe("negative");
    expect(statusTone("CONTRADICTED")).toBe("negative");
    expect(statusTone("STALE")).toBe("neutral");
    expect(statusTone("Unavailable")).toBe("neutral");
  });
  it("turns feed markup and character references into plain text, never HTML", () => {
    expect(plainText("Storage &amp; Peripherals")).toBe("Storage & Peripherals");
    expect(plainText("<p>First paragraph.</p><p>Second&#160;one&#8230;</p>")).toBe("First paragraph. Second one…");
    expect(plainText("It&#x2019;s <a href=\"https://x.test/?a=1&amp;b=2\">here</a>, <em>now</em>.")).toBe("It’s here, now.");
    expect(plainText('<time datetime="2026-07-22T14:38:46-04:00">July 22</time><br/>Line')).toBe("July 22 Line");
    expect(plainText("<script>alert(1)</script><style>p{}</style><!-- note -->Text")).toBe("Text");
    // Escaped markup is decoded once and stays text for React to escape.
    expect(plainText("&lt;b&gt;bold&lt;/b&gt;")).toBe("<b>bold</b>");
    // Comparisons and unknown or invalid references are not markup.
    expect(plainText("Revenue <5% and a < b > c")).toBe("Revenue <5% and a < b > c");
    expect(plainText("AT&T &bogus; &#0; &#xD800;")).toBe("AT&T &bogus; &#0; &#xD800;");
    expect(plainText("  spaced \n\t out  ")).toBe("spaced out");
    expect(plainText(null)).toBe("");
    expect(plainText(42)).toBe("");
  });
  it("humanizes enum labels", () => {
    expect(humanize("AUTH_ERROR")).toBe("AUTH ERROR");
  });
  it("sentence-cases machine keys and slugs, keeping acronyms", () => {
    expect(labelCase("policy_comparison")).toBe("Policy comparison");
    expect(labelCase("crypto-regulation")).toBe("Crypto regulation");
    expect(labelCase("us-inflation")).toBe("US inflation");
    expect(labelCase("fed-hawkish")).toBe("Fed hawkish");
    expect(labelCase("CRYPTO_REGULATION")).toBe("Crypto regulation");
    expect(labelCase("hypotheses")).toBe("Hypotheses");
  });
  it("groups days in Europe/Bucharest, not UTC", () => {
    // 22:30 UTC on 26 Sep is already 27 Sep in Bucharest (UTC+3 in summer).
    expect(localDay("2026-09-26T22:30:00Z")).toContain("27 September");
    expect(localDay("not a date")).toBe("Date unavailable");
  });
  it("cn merges conflicting Tailwind classes", () => {
    expect(cn("px-2 text-[10px]", "px-1", false && "hidden")).toBe(
      "text-[10px] px-1",
    );
    // Column classes may soften the table cell's wrap-anywhere.
    expect(cn("wrap-anywhere", "wrap-break-word")).toBe("wrap-break-word");
  });
});
