// The dependency Fetch dialog (docs/design/packages.md, Confirmation token): an operation over HTTP that would fetch
// from another machine answers 409 needs_fetch with its plan, and this dialog shows that plan (each URL in full, the
// commit, the tree, the size and the licence, and a warning for a host the server does not fetch from without
// asking). It asks the server for a confirmation token only after the person clicks Fetch; the token is for exactly
// that operation and those fetches, once. Then the operation runs as a job on the server and the dialog follows it,
// with approximate progress. A fetch that shows more to fetch (a plain git dependency's manifest naming another) ends
// with the next plan, which the person confirms again.
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, X } from "lucide-react";
import { api, needsFetchOf, type DepFetchJob, type FetchPlan, type FetchRow } from "./api";
import { apiErrorText, msg, resolveText, useI18n, type UiText } from "./i18n";

/** A size in bytes in words (B, kB, MB, GB). */
export const sizeText = (n: number): string => {
  const units = ["B", "kB", "MB", "GB"];
  let v = n, i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
};

/** The plan a failed job carries (code needs_fetch), or null. */
const nextPlan = (j: DepFetchJob): FetchPlan | null =>
  j.state === "failed" && j.code === "needs_fetch" && j.plan && Array.isArray(j.plan.fetches) ? j.plan : null;

export function FetchConfirmDialog({ plan: first, onClose, onDone, pollMs = 1000 }: {
  plan: FetchPlan;
  onClose: () => void;
  onDone: (result: unknown) => void | Promise<void>;   // the operation ran: what its route would have returned
  pollMs?: number;
}) {
  const { t } = useI18n();
  const [plan, setPlan] = useState<FetchPlan>(first);
  const [again, setAgain] = useState(false);          // a second round: the fetch showed more to fetch
  const [job, setJob] = useState<DepFetchJob | null>(null);
  const [err, setErr] = useState<UiText>("");
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);

  // Esc closes wherever the focus is (the dialog.tsx rule), and never reaches the editor behind
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      close.current();
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, []);

  const ended = async (j: DepFetchJob) => {
    if (j.state === "done") { await onDone(j.result); return; }
    const next = nextPlan(j);
    if (next) { setPlan(next); setJob(null); setAgain(true); }   // shown again: the person clicks Fetch again
  };

  const follow = async (id: string) => {
    while (live.current) {
      await new Promise((r) => setTimeout(r, pollMs));
      if (!live.current) return;
      let j: DepFetchJob;
      try { j = await api.depsFetchStatus(plan.project, id); } catch (e) { setErr(apiErrorText(e)); return; }
      if (!live.current) return;
      setJob(j);
      if (j.state === "done" || j.state === "failed") { await ended(j); return; }
    }
  };

  const start = async () => {
    if (busy) return;
    setBusy(true);
    setErr("");
    try {
      // the token only now, after the click, and for the plan shown
      const { token } = await api.confirmDependencyFetch(plan);
      const r = await api.depsFetch(plan.project, token);
      setJob(r.job);
      if (r.job.state === "done" || r.job.state === "failed") await ended(r.job);
      else void follow(r.job.id);
    } catch (e) {
      // the plan changed since it was shown (the server made it again): show the new one
      const next = needsFetchOf(e);
      if (next) { setPlan(next); setAgain(true); } else setErr(apiErrorText(e));
    } finally {
      if (live.current) setBusy(false);
    }
  };

  const running = job !== null && (job.state === "queued" || job.state === "running");
  const row = (label: UiText, value: UiText, mono = true) => (
    <div className="kv"><span>{resolveText(label)}</span><b className={mono ? "mono" : ""}>{resolveText(value)}</b></div>);
  const unknown = msg("app:depFetch.unknown");
  const commitText = (f: FetchRow): UiText => f.rev && f.commit ? msg("app:depFetch.lastSeen", { commit: f.commit })
    : f.commit ?? msg("app:depFetch.commitAfter");
  const p = job?.progress;
  return (
    <div className="modalback" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={t("app:depFetch.title")}>
        <div className="modalhead">
          <span className="grow">{t("app:depFetch.title")}</span>
          <button className="icon" title={t("common:actions.cancelShortcut")} onClick={onClose}><X size={15} /></button>
        </div>
        <div className="modalbody">
          <div className="dim small">{t("app:depFetch.description")}</div>
          {again && <div className="note">{t("app:depFetch.again")}</div>}
          {plan.fetches.map((f) => (
            <div key={`${f.url} ${f.rev ?? f.commit} ${f.subdir ?? ""}`} data-testid="fetch-row">
              {row(msg("app:depFetch.url"), f.url + (f.subdir ? ` (${f.subdir})` : ""))}
              {f.package && row(msg("app:depFetch.package"), f.version ? `${f.package} ${f.version}` : f.package)}
              {f.rev && row(msg("app:depFetch.revision"), f.rev)}
              {row(msg("app:depFetch.commit"), commitText(f), !!f.commit)}
              {row(msg("app:depFetch.tree"), f.tree ?? unknown, !!f.tree)}
              {row(msg("app:depFetch.size"), f.size !== null ? sizeText(f.size) : unknown, false)}
              {row(msg("app:depFetch.license"), f.license ?? unknown, !!f.license)}
              {!f.allowed && <div className="note err" role="alert"><AlertTriangle size={13} /> {t("app:depFetch.hostWarning", { host: f.host ?? f.url })}</div>}
            </div>
          ))}
          {!plan.complete && <div className="faint small">{t("app:depFetch.incomplete")}</div>}
          {job && (
            <>
              <div className={job.state === "failed" ? "note err" : "note"}>
                {running && <Loader2 size={12} className="spin" />} {t(`app:depFetch.state.${job.state}`)}
                {job.error ? `: ${job.error}` : ""}
              </div>
              {running && p && typeof p.files === "number" && typeof p.of === "number" &&
                <div className="dim small" data-testid="fetch-progress">{t("app:depFetch.files", { files: p.files, of: p.of })}</div>}
              {running && p && typeof p.bytes === "number" && typeof p.files !== "number" &&
                <div className="dim small" data-testid="fetch-progress">{t("app:depFetch.bytes", { size: sizeText(p.bytes) })}</div>}
              {running && <div className="faint small">{t("app:depFetch.background")}</div>}
            </>
          )}
          {err && <div className="note err"><AlertTriangle size={13} /> {resolveText(err)}</div>}
          <div className="modalfoot">
            <button type="button" className="ghost" onClick={onClose}>
              {job ? t("common:actions.close") : t("common:actions.cancel")}
            </button>
            {!job && (
              <button type="button" className="primary" disabled={busy} onClick={start}>
                {busy ? t("common:state.working") : t("app:depFetch.start")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
