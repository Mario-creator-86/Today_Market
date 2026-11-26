/**
 * Render an arbitrary API value as display text. Missing values are shown as
 * "Unavailable" and are never coerced to a measured zero.
 */
export function text(value: unknown): string {
  return value === null || value === undefined
    ? "Unavailable"
    : typeof value === "object"
      ? JSON.stringify(value)
      : String(value);
}

const UUID_VALUE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Keys that hold identifiers ("id", "event_id", "evidence_ids"): never text a reader sees. */
function isIdKey(key: string): boolean {
  return key === "id" || key === "ids" || /_ids?$/.test(key);
}

/**
 * Every string/number value of a record (keys, booleans and nulls are not
 * text a reader sees). With `skipIds`, identifier keys and UUID-shaped values
 * are left out as well.
 */
function values(value: unknown, out: string[], skipIds: boolean, depth = 0): string[] {
  if (depth > 6 || value === null || value === undefined) return out;
  if (typeof value === "string") {
    if (!(skipIds && UUID_VALUE.test(value.trim()))) out.push(value);
  } else if (typeof value === "number" && Number.isFinite(value))
    out.push(String(value));
  else if (Array.isArray(value))
    for (const item of value) values(item, out, skipIds, depth + 1);
  else if (typeof value === "object")
    for (const [key, item] of Object.entries(value))
      if (!(skipIds && isIdKey(key))) values(item, out, skipIds, depth + 1);
  return out;
}

/**
 * Case-insensitive list search. Pass `fields` with the text the page actually
 * shows for a record (headline, status label, symbols, source names): the
 * search must not match ids, keys or hidden fields. Without `fields` it falls
 * back to the record's string and number values, never its keys, identifier
 * fields ("id", "*_id") or UUID-shaped values.
 */
export function matchesSearch<T>(
  record: T,
  query: string,
  fields?: (record: T) => unknown[],
): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystack = fields ? values(fields(record), [], false) : values(record, [], true);
  return haystack.join("\n").toLowerCase().includes(needle);
}

/** Named character references seen in feed text; numeric ones ("&#160;", "&#x2019;") are decoded generically. */
const ENTITIES: Record<string, string> = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'",
  nbsp: " ", ensp: " ", emsp: " ", thinsp: " ", shy: "", zwnj: "", zwj: "", lrm: "", rlm: "",
  hellip: "…", mdash: "—", ndash: "–", minus: "−", bull: "•", middot: "·",
  lsquo: "‘", rsquo: "’", sbquo: "‚", ldquo: "“", rdquo: "”", bdquo: "„", laquo: "«", raquo: "»",
  deg: "°", copy: "©", reg: "®", trade: "™", sect: "§", para: "¶", times: "×", divide: "÷", plusmn: "±",
  euro: "€", pound: "£", yen: "¥", cent: "¢", frac12: "½", frac14: "¼", frac34: "¾",
  aacute: "á", agrave: "à", acirc: "â", auml: "ä", atilde: "ã", aring: "å", ccedil: "ç",
  eacute: "é", egrave: "è", ecirc: "ê", euml: "ë", iacute: "í", igrave: "ì", icirc: "î", iuml: "ï",
  ntilde: "ñ", oacute: "ó", ograve: "ò", ocirc: "ô", ouml: "ö", otilde: "õ", oslash: "ø",
  uacute: "ú", ugrave: "ù", ucirc: "û", uuml: "ü", szlig: "ß",
  Aacute: "Á", Eacute: "É", Oacute: "Ó", Uacute: "Ú", Auml: "Ä", Ouml: "Ö", Uuml: "Ü", Ccedil: "Ç", Ntilde: "Ñ",
};

/** Elements whose start or end separates words ("<p>", "<br>", "</li>"); other tags join the text around them. */
const BLOCK_TAG =
  /^(?:address|article|aside|blockquote|br|dd|div|dl|dt|figcaption|figure|footer|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tbody|td|tfoot|th|thead|tr|ul)$/i;
/** A start or end tag: a letter-led name, then attributes (quoted values may hold ">"). "a < b" and "<5%" are not tags. */
const TAG = /<\/?([a-zA-Z][\w:-]*)(?:\s(?:[^<>"']|"[^"]*"|'[^']*')*)?\/?>/g;
const ENTITY = /&(?:#(\d{1,7})|#[xX]([0-9a-fA-F]{1,6})|([a-zA-Z][a-zA-Z0-9]{1,7}));/g;

function codePoint(value: number): string | null {
  const valid = value > 0 && value <= 0x10ffff && !(value >= 0xd800 && value <= 0xdfff);
  return valid ? String.fromCodePoint(value) : null;
}

/**
 * Feed text as plain text: markup stripped (block elements become a space,
 * script/style/comments dropped), character references decoded once
 * ("&amp;" -> "&", "&#160;" -> a space) and whitespace collapsed. For text a
 * page renders as text, never as HTML. Unknown references stay as written.
 */
export function plainText(value: unknown): string {
  if (typeof value !== "string") return "";
  let out = value;
  if (out.includes("<"))
    out = out
      .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, " ")
      .replace(/<!--[\s\S]*?-->/g, "")
      .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
      .replace(TAG, (_tag, name: string) => (BLOCK_TAG.test(name) ? " " : ""));
  if (out.includes("&"))
    out = out.replace(ENTITY, (ref, dec?: string, hex?: string, name?: string) => {
      if (dec) return codePoint(parseInt(dec, 10)) ?? ref;
      if (hex) return codePoint(parseInt(hex, 16)) ?? ref;
      return name && Object.hasOwn(ENTITIES, name) ? ENTITIES[name] : ref;
    });
  return out.replace(/\s+/g, " ").trim();
}

/** "SOME_ENUM_VALUE" -> "SOME ENUM VALUE". */
export function humanize(value: string): string {
  return value.replaceAll("_", " ");
}

/** Words kept upper case by `labelCase`. */
const ACRONYMS = new Set([
  "AI", "AUD", "BOE", "BOJ", "BTC", "CAD", "CHF", "CPI", "ECB", "ETF", "ETH",
  "EU", "EUR", "FOMC", "FX", "GBP", "GDP", "IR", "JPY", "NZD", "OPEC", "PMI",
  "PPI", "SEC", "SOL", "UK", "US", "USD", "VIX", "WTI",
]);
/** Proper names that are not acronyms. */
const NAMES: Record<string, string> = { FED: "Fed" };

/**
 * Sentence case for machine keys and slugs: "policy_comparison" -> "Policy
 * comparison", "us-inflation" -> "US inflation", "crypto-regulation" ->
 * "Crypto regulation". Known acronyms stay upper case.
 */
export function labelCase(value: string): string {
  const words = value.split(/[\s_-]+/).filter(Boolean);
  return words
    .map((word, i) => {
      const upper = word.toUpperCase();
      if (ACRONYMS.has(upper)) return upper;
      if (NAMES[upper]) return NAMES[upper];
      const lower = word.toLowerCase();
      return i === 0 ? lower[0].toUpperCase() + lower.slice(1) : lower;
    })
    .join(" ");
}

/** Finite number guard (null, undefined, booleans and NaN are not numbers). */
export function isNum(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Badge tone of a backend status (same values as the Badge variants). */
export type StatusTone = "positive" | "negative" | "info" | "neutral";

const POSITIVE_STATUS = /^(ONLINE|CONFIRMED|LONG|FRESH|PASS|APPROVED)$/;
const NEGATIVE_STATUS = /^(OFFLINE|AUTH_ERROR|ERROR|SHORT|CONTRADICTED|REJECTED|FALSE)$/;
/** Partial or official support: informative, neither a confirmation nor a warning. */
const INFO_STATUS = /^(PARTIALLY_CONFIRMED|CORROBORATED|OFFICIAL_STATEMENT)$/;

/**
 * Map a backend status label to a badge tone, so one status has one colour on
 * every page. Spaces count as underscores ("PARTIALLY CONFIRMED"). Unknown
 * labels are neutral.
 */
export function statusTone(label: string): StatusTone {
  const key = label.trim().replaceAll(" ", "_");
  return POSITIVE_STATUS.test(key)
    ? "positive"
    : NEGATIVE_STATUS.test(key)
      ? "negative"
      : INFO_STATUS.test(key)
        ? "info"
        : "neutral";
}
