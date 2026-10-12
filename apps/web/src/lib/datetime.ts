/**
 * Timestamp formatting.
 *
 * The backend sends ISO-8601 with an offset, always
 * (`docs/ARCHITECTURE.md` — every timestamp column is `TIMESTAMPTZ`), and the
 * interface renders in the viewer's local zone. That conversion is the whole
 * point of sending an offset, and doing it per call site is how two screens
 * end up disagreeing about what "14:02" means.
 *
 * ## Absent is not zero, and not now
 *
 * `last_login_at` is null for an account that has never signed in, and the one
 * thing these functions must never do is render that as a date. Each takes
 * `string | null` and returns an explicit placeholder, so a caller cannot
 * accidentally pass a null into a formatter that would answer "1 January
 * 1970" — the dated-looking wrong answer being much worse than a dash.
 *
 * No `Intl.RelativeTimeFormat` for absolute facts: "3 months ago" is friendly
 * for recent activity and useless for "when was this account created", where
 * the operator wants a date they can compare against a change they remember.
 */

/** Rendered in place of a missing timestamp. An em dash, not an empty cell. */
export const ABSENT = '—';

const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
});

const DATE_ONLY = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });

/**
 * Parse an ISO timestamp, returning `null` for anything unusable.
 *
 * A malformed value from the network produces an `Invalid Date`, which
 * `Intl.DateTimeFormat` renders as the literal string "Invalid Date" — a
 * value that looks like a bug in the page rather than in the data. Returning
 * null routes it to the same placeholder as an absent one.
 */
function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** Date and time, in the viewer's zone. */
export function formatDateTime(value: string | null | undefined): string {
  const parsed = parse(value);
  return parsed === null ? ABSENT : DATE_TIME.format(parsed);
}

/** Date only, for a creation date where the time adds nothing. */
export function formatDate(value: string | null | undefined): string {
  const parsed = parse(value);
  return parsed === null ? ABSENT : DATE_ONLY.format(parsed);
}

/**
 * The machine-readable value for a `<time dateTime=…>` attribute.
 *
 * Returns `undefined` rather than an empty string when there is no timestamp,
 * so the attribute is omitted instead of present-and-empty — an empty
 * `dateTime` is invalid and some assistive technology reads it as a value.
 */
export function machineDateTime(value: string | null | undefined): string | undefined {
  return parse(value) === null ? undefined : (value ?? undefined);
}
