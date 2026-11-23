// The review of a changeset's dependency operations (docs/design/packages.md, Changesets and evidence): per operation
// the lock diff and project.json's, the licence and the download size, the update report (packages changed, their
// cells, the project's cells that use them), the package's code blocks and RTL files with what the trust rule refuses,
// the trust note, and "unchecked" until a run in the changeset read the package. An agent's operation waiting for a
// fetch has its Fetch button here. Everything that came from a package is shown as text (the server took control
// characters out and cut it): data written by others.
import { AlertTriangle, Download, ShieldCheck } from "lucide-react";
import type { DepReviewCode, DepReviewPackage, DepReviewRow, PendingDep } from "../api";
import { sizeText } from "../FetchConfirmDialog";
import { t, useI18n } from "../i18n";
import { DepDiffView } from "./gitViews";

const STATES = new Set(["installed", "needs_fetch", "cancelled"]);
const KINDS = ["control", "control_line", "include", "lib", "file", "tcl", "unparsed"] as const;
const blockKind = (k: string): (typeof KINDS)[number] | "other" =>
  (KINDS as readonly string[]).includes(k) ? k as (typeof KINDS)[number] : "other";

function rowState(r: DepReviewRow): string {
  if (r.outcome) return t(`review:deps.outcome.${r.outcome}`);
  return t(`review:deps.state.${STATES.has(r.state) ? r.state : "installed"}`);
}

function rowTitle(r: DepReviewRow): string {
  if (r.action === "pdk_set") return t("review:deps.pdk", { from: r.pdk?.[0] ?? "—", to: r.pdk?.[1] ?? "—" });
  const action = r.action ?? "update";
  return t("review:deps.row", { action: t(`review:deps.action.${action}`), name: r.name ?? "" });
}

function PackageReport({ p }: { p: DepReviewPackage }) {
  useI18n();
  const name = p.package ?? p.names[0] ?? "";
  const ch = p.changed;
  return (
    <div className="deppkg" data-testid="dep-package">
      <div className="small">
        <b className="mono">{name}</b>{" "}
        {p.do === "add" ? t("review:deps.pkg.added", { to: p.to ?? "" })
          : p.do === "del" ? t("review:deps.pkg.removed", { from: p.from ?? "" })
          : t("review:deps.pkg.moved", { from: p.from ?? "", to: p.to ?? "" })}
        {p.names.length > 1 && <span className="dim"> · {t("review:deps.pkg.names", { names: p.names.join(", ") })}</span>}
      </div>
      {p.do !== "del" && (
        <div className="small">{p.license ?? t("review:pending.licenseUnknown")}
          {" · "}{p.size !== null && p.size !== undefined ? t(p.size_from === "index" ? "review:deps.sizeDownload"
            : "review:deps.sizeFiles", { size: sizeText(p.size) }) : t("review:pending.sizeUnknown")}
          {!!p.license_files?.length && <span className="dim"> · {p.license_files.join(", ")}</span>}</div>
      )}
      {ch ? (
        <div className="small dim">{t("review:deps.files", {
          added: ch.added.length + (ch.added_more ?? 0), removed: ch.removed.length + (ch.removed_more ?? 0),
          modified: ch.modified.length + (ch.modified_more ?? 0) })}</div>
      ) : <div className="small dim">{t("review:deps.filesUnknown")}</div>}
      {!!p.cells?.length && (
        <ul className="depcells small">
          {p.cells.map((c) => (
            <li key={c.cell} className={`fx fx-${c.do === "renamed" ? "mod" : c.do}`}>
              <span className="mono">{c.cell}</span>{" "}
              {c.do === "renamed" ? t("review:deps.cell.renamed", { from: c.from ?? "" }) : t(`review:deps.cell.${c.do}`)}
              {c.do === "mod" && !!c.views.length && <> · {t("review:deps.cell.views", { views: c.views.join(", ") })}</>}
              {c.exported === false && <span className="dim"> · {t("review:deps.cell.internal")}</span>}
            </li>
          ))}
          {!!p.cells_more && <li className="dim">{t("review:deps.more", { count: p.cells_more })}</li>}
        </ul>
      )}
    </div>
  );
}

function CodeList({ c }: { c: DepReviewCode }) {
  useI18n();
  if (c.error) return <div className="small warn">{c.error}</div>;
  const blocks = c.blocks ?? [], rtl = c.rtl ?? [];
  const total = (c.blocks_total ?? 0) + (c.rtl_total ?? 0);
  return (
    <div className="depcode" data-testid="dep-code">
      <div className="small">
        <span className="mono">{c.key ?? c.name}</span>{" · "}
        <span className={c.trusted ? "okc" : (c.blocks_refused || c.rtl_refused) ? "warn" : "dim"}>
          {c.trusted ? <><ShieldCheck size={12} /> {t("review:deps.trust.trusted")}</>
            : c.taken_back ? <span data-testid="dep-code-taken-back">{t("review:deps.trust.takenBack")}</span> : t("review:deps.trust.untrusted")}
        </span>
      </div>
      <div className="small">{t("review:deps.code.counts", { blocks: c.blocks_total ?? 0, blocksRefused: c.blocks_refused ?? 0,
        rtl: c.rtl_total ?? 0, rtlRefused: c.rtl_refused ?? 0 })}</div>
      {total > 0 && (
        <details>
          <summary className="small dim">{t("review:deps.code.show")}</summary>
          {blocks.map((b, i) => (
            <div key={`b${i}`} className={`small depblock ${b.refused ? "badc" : "dim"}`}>
              <span className="mono">{b.file}:{b.line}</span> {t(`review:deps.code.kind.${blockKind(b.kind)}`)}
              {b.commands?.length ? <> · <span className="mono">{b.commands.join(" ")}</span></> : null}
              {b.path && <> · <span className="mono">{b.path}</span></>}
              <div className="mono depquote">{b.text}</div>
            </div>
          ))}
          {blocks.length < (c.blocks_total ?? 0) && <div className="small dim">{t("review:deps.more", { count: (c.blocks_total ?? 0) - blocks.length })}</div>}
          {rtl.map((f) => (
            <div key={f.path} className={`small depblock ${f.refused ? "badc" : "dim"}`}>
              <span className="mono">{f.path}</span> {f.refused ? t("review:deps.code.rtlRefused") : t("review:deps.code.rtlClean")}
              {f.findings.map((x, i) => <div key={i} className="mono depquote">{x.line}: {x.text}</div>)}
              {!!f.findings_more && <div className="dim">{t("review:deps.more", { count: f.findings_more })}</div>}
            </div>
          ))}
          {rtl.length < (c.rtl_total ?? 0) && <div className="small dim">{t("review:deps.more", { count: (c.rtl_total ?? 0) - rtl.length })}</div>}
        </details>
      )}
    </div>
  );
}

/** One dependency operation of the review. `pending`: its operation waiting for a person's fetch (the Fetch button). */
function DepRow({ r, pending, onFetch, busy, status }: {
  r: DepReviewRow; pending?: PendingDep; onFetch: (p: PendingDep) => void; busy: boolean; status: string;
}) {
  useI18n();
  const size = r.size !== null && r.size !== undefined ? t(r.size_from === "index" ? "review:deps.sizeDownload" : "review:deps.sizeFiles",
    { size: sizeText(r.size) }) : t("review:pending.sizeUnknown");
  const inactive = r.state === "cancelled" || !!r.outcome;
  // more than one package (or fetch) comes with it: its licence and size above are its own; the total, and each
  // package's in its report
  const count = r.state === "installed" ? r.packages.filter((p) => p.do !== "del").length + (r.packages_more ?? 0)
    : (r.needs?.length ?? 0) + (r.needs_more ?? 0);
  // the named dependency did not move itself (only packages it needs did): no licence or size is its own
  const notOwn = r.own === false && count > 0;
  const several = count > 1 || notOwn;
  // not trusted in the project now (a run of their code is refused now); those trusted now that the change takes back
  // are said on their own line (an older server gives only `untrusted`)
  const back = r.trust_taken_back ?? [];
  const nowKeys = r.untrusted_now ?? (r.untrusted ?? []).filter((k) => !back.includes(k));
  const nowCount = r.untrusted_now ? Math.max(nowKeys.length, (r.untrusted_count ?? 0) - back.length) : nowKeys.length;
  return (
    <div className={`deprow${inactive ? " dim" : ""}`} data-testid="dep-row">
      <div className="row">
        <b className="mono grow">{rowTitle(r)}</b>
        <span className="chip">{rowState(r)}</span>
        {r.checked === false && !inactive && <span className="chip warn" title={t("review:deps.uncheckedHint")} data-testid="dep-unchecked">
          <AlertTriangle size={11} /> {t("review:deps.unchecked")}</span>}
        {r.checked === null && r.replaced_by != null && <span className="chip" title={t("review:deps.replacedHint")}
          data-testid="dep-replaced">{t("review:deps.replaced")}</span>}
        {r.checked === true && <span className="chip okc" title={r.checked_by ? t("review:deps.checkedBy", {
          job: r.checked_by.job, cell: r.checked_by.lib && r.checked_by.cell ? `${r.checked_by.lib}/${r.checked_by.cell}` : "—" }) : undefined}>
          {t("review:deps.checked")}</span>}
      </div>
      {r.action !== "pdk_set" && r.action !== "remove" && (
        <div className="small">{notOwn ? <span data-testid="dep-not-own">{t(r.state === "installed" ? "review:deps.notOwn" : "review:deps.notOwnFetch")}</span>
          : <>{r.license ?? t("review:pending.licenseUnknown")} · {size}</>}
          {several && <span data-testid="dep-total"> · {r.size_total !== null && r.size_total !== undefined
            ? t("review:deps.sizeTotal", { size: sizeText(r.size_total), count: count })
            : t("review:deps.sizeTotalUnknown", { count })}</span>}
        </div>
      )}
      {r.reason && <div className="opreason">“{r.reason}” <span className="dim">— {r.actor}{r.fetched_by ? ` · ${t("review:deps.fetchedBy", { who: r.fetched_by })}` : ""}</span></div>}
      {r.review_error && <div className="small warn">{r.review_error}</div>}
      {r.review_state === "pending" && <div className="small dim" data-testid="dep-pending-review">{t("review:deps.reviewPending")}</div>}
      {r.state === "needs_fetch" && (
        <div className="row">
          <div className="grow">
            {(r.needs ?? []).map((f) => (
              <div key={`${f.url} ${f.rev ?? f.commit ?? ""}`} className="small" data-testid="dep-need">
                <span className="mono">{f.url}{f.package && f.version ? ` (${f.package} ${f.version})` : ""}</span>
                {" · "}{f.size !== null ? t("review:deps.sizeDownload", { size: sizeText(f.size) }) : t("review:pending.sizeUnknown")}
                {" · "}{f.license ?? t("review:pending.licenseUnknown")}
              </div>
            ))}
            {!!r.needs_more && <div className="small dim">{t("review:deps.more", { count: r.needs_more })}</div>}
            <div className="small dim">{t("review:deps.notFetched")}</div>
          </div>
          {pending && <button className="ghost" disabled={busy} title={t("review:pending.fetchHint")}
            onClick={() => onFetch(pending)}><Download size={13} /> {t("review:pending.fetch")}</button>}
        </div>
      )}
      {!!r.lock.changes.length && <><div className="small dim">{t("review:deps.lockDiff")}</div><DepDiffView diff={r.lock} /></>}
      {r.lock.error && <DepDiffView diff={r.lock} />}
      {(!!r.project.changes.length || r.project.error) && (
        <details><summary className="small dim">{t("review:deps.projectDiff", { count: r.project.changes.length })}</summary><DepDiffView diff={r.project} /></details>
      )}
      {!!r.packages.length && r.action !== "add" && <div className="small dim">{t("review:deps.report")}</div>}
      {r.packages.map((p) => <PackageReport key={`${p.package}|${p.names.join(",")}`} p={p} />)}
      {!!r.packages_more && <div className="small dim">{t("review:deps.more", { count: r.packages_more })}</div>}
      {!!r.used_by.length && (
        <div className="small">
          <div className="dim">{t(r.action === "remove" ? "review:deps.usedByRemoved" : "review:deps.usedBy")}</div>
          {r.used_by.map((u) => <div key={u} className="mono">{u}</div>)}
          {!!r.used_by_more && <div className="dim">{t("review:deps.more", { count: r.used_by_more })}</div>}
        </div>
      )}
      {!!r.code?.length && <>
        <div className="small dim">{t("review:deps.code.title")}</div>
        {r.code.map((c) => <CodeList key={c.name} c={c} />)}
        {!!r.code_more && <div className="small dim">{t("review:deps.codeMore", { count: r.code_more })}</div>}
      </>}
      {!!r.trust_taken_back?.length && <div className="small warn" data-testid="dep-taken-back">
        {t("review:deps.trustTakenBack", { keys: r.trust_taken_back.join(", ") })}</div>}
      {r.trust_unknown && <div className="small warn">{t("review:deps.trustUnknown")}</div>}
      {!!nowKeys.length && <div className="small warn" data-testid="dep-untrusted">
        {t("review:deps.untrustedKeys", { count: nowCount, keys: nowKeys.join(", ") })}</div>}
      {/* the keys it takes out of project.json's trusted: while open the adopt will take them back (said above for a
          version trusted today); once adopted, taken back */}
      {!!r.trust_drop.length && (status === "open" || status === "applied") && <div className="small warn" data-testid="dep-trust-drop">
        {t(status === "open" ? "review:deps.trustDropOpen" : "review:deps.trustDrop", { keys: r.trust_drop.join(", ") })}</div>}
    </div>
  );
}

/** The Dependencies section of a changeset's review: one row per dependency operation in effect, oldest first. */
export function DepReview({ rows, pending, open, busy, onFetch, status }: {
  rows: DepReviewRow[]; pending: PendingDep[]; open: boolean; busy: boolean; onFetch: (p: PendingDep) => void;
  status?: string;
}) {
  useI18n();
  if (!rows.length) return null;
  const byOp = new Map(pending.map((p) => [p.op_id, p]));
  return (
    <div className="depreview" data-testid="dep-review">
      <div className="section">{t("review:deps.title")}</div>
      <div className="small dim">{t("review:deps.note")}</div>
      {rows.map((r) => <DepRow key={`${r.gseq}|${r.op_id ?? ""}|${r.name ?? ""}`} r={r} status={status ?? (open ? "open" : "closed")}
        pending={open && r.op_id ? byOp.get(r.op_id) : undefined} onFetch={onFetch} busy={busy} />)}
    </div>
  );
}
