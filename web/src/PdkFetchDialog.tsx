// The PDK Fetch dialog (docs/design/packages.md, Confirmation token): it shows what `ciel fetch` would run (family,
// variant, commit, libraries, source and the root it installs under) and asks the server for a confirmation token
// only after the person clicks Fetch. The token is for exactly that plan, once; then the fetch runs as a job on the
// server, and the dialog follows its log until it is done.
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, X } from "lucide-react";
import { api, type PdkFetchJob, type PdkFetchPlan } from "./api";
import { apiErrorText, msg, resolveText, useI18n, type UiText } from "./i18n";

const STATE_KEYS = { verified: "verified", unverified: "unverified", modified: "modified", missing: "missing",
  unchecked: "unchecked", "not pinned": "notPinned", "other version": "otherVersion", mismatch: "mismatch",
  error: "error", "not locked": "notLocked", none: "none" } as const;

/** A PDK state (pdk_block / pin.verify) in words, or as the server said it when this UI has no words for it. */
export const pdkStateText = (state: string): UiText => {
  const key = STATE_KEYS[state as keyof typeof STATE_KEYS];
  return key ? msg(`app:pdk.state.${key}`) : state;
};

export function PdkFetchDialog({ project, onClose, onFetched, pollMs = 1000 }: {
  project: string;
  onClose: () => void;
  onFetched: () => void | Promise<void>;   // the job is done: the PDK list, the project and its lock status read again
  pollMs?: number;
}) {
  const { t } = useI18n();
  const [network, setNetwork] = useState(false);
  const [plan, setPlan] = useState<PdkFetchPlan | null>(null);
  const [err, setErr] = useState<UiText>("");
  const [job, setJob] = useState<PdkFetchJob | null>(null);
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);

  // the plan: read only; with the network box ticked the server may run `ciel ls-remote` to expand a 12-character id
  useEffect(() => {
    setPlan(null);
    setErr("");
    api.pdkFetchPlan(project, network).then((p) => { if (live.current) setPlan(p); })
      .catch((e) => { if (live.current) setErr(apiErrorText(e)); });
  }, [project, network]);

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

  const follow = async (id: string) => {
    while (live.current) {
      await new Promise((r) => setTimeout(r, pollMs));
      if (!live.current) return;
      let j: PdkFetchJob;
      try { j = await api.pdkFetchStatus(project, id); } catch (e) { setErr(apiErrorText(e)); return; }
      if (!live.current) return;
      setJob(j);
      if (j.state === "done") { await onFetched(); return; }
      if (j.state === "failed") return;
    }
  };

  const start = async () => {
    if (!plan || busy) return;
    setBusy(true);
    setErr("");
    try {
      // the token only now, after the click, and for the plan shown
      const { token } = await api.confirmPdkFetch(project, { commit: plan.commit, variant: plan.variant,
        libraries: plan.libraries, allow_network: network });
      const r = await api.pdkFetch(project, token);
      setJob(r.job);
      if (r.job.state === "done") await onFetched();
      else if (r.job.state !== "failed") void follow(r.job.id);
    } catch (e) {
      setErr(apiErrorText(e));
    } finally {
      if (live.current) setBusy(false);
    }
  };

  const running = job !== null && (job.state === "queued" || job.state === "running");
  const row = (label: UiText, value: UiText, mono = true) => (
    <div className="kv"><span>{resolveText(label)}</span><b className={mono ? "mono" : ""}>{resolveText(value)}</b></div>);
  const verify = job?.result?.verify;
  return (
    <div className="modalback" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={t("app:pdkFetch.title")}>
        <div className="modalhead">
          <span className="grow">{t("app:pdkFetch.title")}</span>
          <button className="icon" title={t("common:actions.cancelShortcut")} onClick={onClose}><X size={15} /></button>
        </div>
        <div className="modalbody">
          <div className="dim small">{t("app:pdkFetch.description")}</div>
          {plan ? (
            <div>
              {row(msg("app:pdkFetch.family"), plan.family)}
              {row(msg("app:pdkFetch.variant"), plan.variant)}
              {row(msg("app:pdkFetch.commit"), plan.commit)}
              {row(msg("app:pdkFetch.libraries"), plan.libraries ? plan.libraries.join(", ") : msg("app:pdkFetch.cielDefault"), !!plan.libraries)}
              {row(msg("app:pdkFetch.source"), plan.source)}
              {row(msg("app:pdkFetch.root"), plan.root)}
            </div>
          ) : !err && <div className="dim small"><Loader2 size={12} className="spin" /> {t("app:pdkFetch.planning")}</div>}
          <label className="checkopt">
            <input type="checkbox" checked={network} disabled={job !== null || busy} onChange={(e) => setNetwork(e.target.checked)} />
            {t("app:pdkFetch.network")}
          </label>
          {job && (
            <>
              <div className={job.state === "failed" ? "note err" : "note"}>
                {running && <Loader2 size={12} className="spin" />} {t(`app:pdkFetch.state.${job.state}`)}
                {job.error ? `: ${job.error}` : ""}
              </div>
              {job.log.length > 0 && <pre className="logbox" data-testid="pdk-fetch-log">{job.log.join("\n")}</pre>}
              {verify && <div className="dim small">{t("app:pdkFetch.verify", { state: resolveText(pdkStateText(verify.state)) })}</div>}
              {verify?.warnings?.map((w) => <div key={w} className="faint small">{w}</div>)}
              {running && <div className="faint small">{t("app:pdkFetch.background")}</div>}
            </>
          )}
          {err && <div className="note err"><AlertTriangle size={13} /> {resolveText(err)}</div>}
          <div className="modalfoot">
            <button type="button" className="ghost" onClick={onClose}>
              {job ? t("common:actions.close") : t("common:actions.cancel")}
            </button>
            {!job && (
              <button type="button" className="primary" disabled={!plan || busy} onClick={start}>
                {busy ? t("common:state.working") : t("app:pdkFetch.start")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
