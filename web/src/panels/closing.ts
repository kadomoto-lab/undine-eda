// A changeset being adopted, discarded or reverted on the server (server/undine/server/live.py, Closings) as the Changes
// list, the review and the top bar say it ("adopting: saving evidence 3.1 / 25 GB"), and the sizes of evidence.
import type { ChangesetClosing } from "../api";
import { msg, type UiText } from "../i18n";

const UNITS = ["B", "kB", "MB", "GB", "TB"];
const scale = (n: number) => { let i = 0; while (n >= 1000 ** (i + 1) && i < UNITS.length - 1) i++; return i; };
const digits = (v: number) => (v >= 100 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1));

/** Bytes as people read them: 512 B, 12 kB, 820 MB, 3.1 GB (powers of 1000). */
export function formatBytes(n: number): string {
  const i = scale(n);
  return `${digits(n / 1000 ** i)} ${UNITS[i]}`;
}

/** Bytes done of a total, in the total's unit: "3.1 / 25 GB". */
export function formatBytesOf(done: number, total: number): string {
  const i = scale(total);
  return `${digits(Math.min(done, total) / 1000 ** i)} / ${digits(total / 1000 ** i)} ${UNITS[i]}`;
}

/** What the closure is doing: waiting for another (named), committing, saving evidence (bytes done of how many),
 * merging, cleaning up. */
export function closingPhase(c: ChangesetClosing): UiText {
  switch (c.phase) {
    case "waiting":
      return c.behind ? msg("review:closing.waiting", { title: c.behind.title || c.behind.id, action: msg(`review:status.closing.${c.behind.action}`) })
        : msg("review:closing.waitingOther");
    case "committing": return msg("review:closing.committing");
    case "evidence":
      return c.bytes_total ? msg("review:closing.evidenceBytes", { ...splitBytes(c.bytes ?? 0, c.bytes_total) }) : msg("review:closing.evidence");
    case "merging": return msg("review:closing.merging");
    default: return msg("review:closing.cleanup");
  }
}

function splitBytes(done: number, total: number) {
  const [d, rest] = formatBytesOf(done, total).split(" / ");
  return { done: d, total: rest };
}

/** "adopting: saving evidence 3.1 / 25 GB". */
export function closingText(c: ChangesetClosing): UiText {
  return msg("review:closing.line", { action: msg(`review:status.closing.${c.action}`), phase: closingPhase(c) });
}
