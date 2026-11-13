import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Check, Cloud, GitPullRequestArrow, ImagePlus, Loader2, Send, Server, Settings2, Square, X } from "lucide-react";
import { hashParam } from "../App";
import { api, ChatCtx, ChatImage, ChatItem, ChatTurn, Conversation, LlmStatus, TurnEvent, TurnState } from "../api";
import { Splitter } from "../layout";
import { confirmDialog } from "../dialog";
import { WorkTarget, workTarget } from "./work";
import { ConnectionSettings } from "./ConnectionSettings";
import { apiErrorText, msg, t, useI18n, type UiText } from "../i18n";

export type { WorkTarget };

export interface Selection { lib: string; cell: string; view: string; items: ChatItem[] }

interface Props {
  project: string;
  target: { lib: string; cell: string; view: string } | null; // the open cell
  selection: Selection | null;
  onClose: () => void;
  onReview: (changesetId: string) => void;
  onProposed?: () => void; // a propose turn finished: the working copy may have changed
  /** A running turn committed changes to `changeset` (targets: the cells it changed, when known). */
  onTurnWork?: (turn: string, changeset: string, targets: WorkTarget[]) => void;
  follow?: { paused: boolean; onPause: () => void; onResume: () => void }; // the drawing follows the agent
  focusNonce?: number;     // changes when the person asks to request changes: the message box gets the focus
  docked?: boolean; // beside the drawing (during a review) instead of over it
  capture: (() => ChatImage | null) | null; // a picture of the selection / view; only offered in the layout view
  toast: (m: UiText, k?: "error" | "info") => void;
}

const args = (input?: Record<string, unknown>) =>
  Object.entries(input ?? {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ").slice(0, 120);

/** A turn while it runs: what the assistant says and does, step by step (server/undine/agents/turns.py). */
function LiveTurn({ events, state, request, onStop, onReview, follow }: {
  events: TurnEvent[]; state: TurnState; request: string; onStop: () => void; onReview: (id: string) => void; follow?: Props["follow"];
}) {
  useI18n();
  const tools = new Map<string, TurnEvent>();
  const rows: TurnEvent[] = [];
  for (const e of events) {
    if (e.type === "tool" && e.id) {
      const first = tools.get(e.id);
      if (first) Object.assign(first, { state: e.state, result: e.result, gseq: e.gseq });
      else { const row = { ...e }; tools.set(e.id, row); rows.push(row); }
    } else if (e.type !== "state") rows.push(e);
  }
  return (
    <div className="msg assistant live">
      <div className="who"><Bot size={12} /> {t("agent:title")} <span className="dim ellip">· {state === "stopping" ? t("agent:live.stopping")
        : t("agent:live.working", { request: request.length > 60 ? request.slice(0, 60) + "…" : request })}</span></div>
      {rows.map((e) => e.type === "text" ? <div key={e.i} className="body">{e.text}</div>
        : e.type === "changeset" ? (
          <button key={e.i} className="csjump" onClick={() => onReview(e.id!)}>
            <GitPullRequestArrow size={14} /> {t("agent:live.proposal", { id: e.id ?? "" })}
          </button>
        ) : (
          <div key={e.i} className={`action ${e.state === "error" ? "err" : ""}`}>
            {e.state === "running" ? <Loader2 size={12} className="spin" /> : e.state === "error" ? <X size={12} className="badc" /> : <Check size={12} className="okc" />}
            <span className="mono">{e.name}</span>
            <span className="dim small">{args(e.input)}</span>
            {e.reason && <span className="small">“{e.reason}”</span>}
            {e.state === "error" && <span className="small badc">{e.result}</span>}
          </div>
        ))}
      <div className="row">
        <span className="spinner" /><span className="grow" />
        {follow && (follow.paused
          ? <button className="ghost" onClick={follow.onResume} title={t("agent:follow.resumeHint")}>{t("agent:follow.resume")}</button>
          : <button className="ghost" onClick={follow.onPause} title={t("agent:follow.pauseHint")}>{t("agent:follow.pause")}</button>)}
        <button className="ghost" disabled={state === "stopping"} onClick={onStop} title={t("agent:stopHint")}>
          <Square size={12} /> {t("agent:stop")}
        </button>
      </div>
    </div>
  );
}

function Message({ m, onReview, project }: { m: ChatTurn; onReview: (id: string) => void; project: string }) {
  useI18n();
  const parts = m.text.split(/```/);
  return (
    <div className={`msg ${m.role}`}>
      <div className="who">{m.role === "user" ? t("agent:you") : <><Bot size={12} /> {t("agent:title")}</>}
        {m.role === "assistant" && m.state && m.state !== "done" && <span className="pill warn">{t(`agent:state.${m.state}`)}</span>}</div>
      <div className="body">
        {parts.map((p, i) => (i % 2 ? <pre key={i} className="logbox">{p.replace(/^\w*\n/, "")}</pre> : <span key={i}>{p}</span>))}
      </div>
      {m.role === "assistant" && !!m.actions?.length && (
        <details className="sent actions">
          <summary>{t("agent:steps", { count: m.actions.length })}</summary>
          {m.actions.map((a, i) => (
            <div key={i} className={`action ${a.error ? "err" : ""}`}>
              <span className="mono">{a.tool}</span>
              <span className="dim small">{args(a.input)}</span>
              {a.reason && <span className="small">“{a.reason}”</span>}
              {a.error && <span className="small badc">{a.result}</span>}
            </div>
          ))}
        </details>
      )}
      {m.role === "assistant" && m.changeset && (
        <button className="csjump" onClick={() => onReview(m.changeset!)}>
          <GitPullRequestArrow size={14} /> {t("agent:review")} <span className="mono dim">{m.changeset}</span>
        </button>
      )}
      {m.role === "user" && !!m.images?.length && (
        <div className="thumbs">
          {m.images.map((im) => <a key={im.sha1} href={api.llmImageUrl(project, im.sha1)} target="_blank" rel="noreferrer" title={t("agent:image.sent", { label: im.label })}><img src={api.llmImageUrl(project, im.sha1)} alt={im.label} /></a>)}
        </div>
      )}
      {m.role === "user" && m.summary && (
        <details className="sent">
          <summary>{t("agent:context.sent", { objects: m.summary.objects.length ? m.summary.objects.join(", ") : t("agent:context.overview"), count: m.summary.chars })}</summary>
          <div className="small dim">{t("agent:context.audit", { cell: m.summary.cell, sections: m.summary.sections.join(", ") || t("agent:context.none") })}</div>
        </details>
      )}
    </div>
  );
}

export function ChatPanel({ project, target, selection, onClose, onReview, onProposed, onTurnWork, follow, focusNonce, docked, capture, toast }: Props) {
  useI18n();
  const [images, setImages] = useState<ChatImage[]>([]);
  const [st, setSt] = useState<LlmStatus | null>(null);
  const [conv, setConv] = useState<Conversation | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [settings, setSettings] = useState(false);
  const [overview, setOverview] = useState(true);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [pin, setPin] = useState<{ revision: string | null; key: string }>({ revision: null, key: "" });
  const [stale, setStale] = useState(false);
  const [live, setLive] = useState<{ turn: string; events: TurnEvent[]; state: TurnState } | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const wantFocus = useRef(0);
  useEffect(() => { if (focusNonce) wantFocus.current = focusNonce; }, [focusNonce]);

  const loadStatus = useCallback(() => api.llm().then((s) => {
    setSt(s);
  }).catch((e) => toast(apiErrorText(e), "error")), [toast]);
  useEffect(() => { loadStatus(); }, [loadStatus]);
  useEffect(() => { const id = hashParam("chat"); if (id && id !== "1") api.chatGet(project, id).then(setConv).catch(() => {}); }, [project]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [conv?.messages.length, busy]);

  // the selection that applies to the open cell, pinned to the design revision at the time it was made
  const items = (selection && target && selection.lib === target.lib && selection.cell === target.cell ? selection.items : []);
  const chips = items.filter((i) => !excluded.has(`${i.k}:${i.id ?? i.desc}`));
  const selKey = JSON.stringify(items.map((i) => [i.k, i.id ?? i.desc])) + (target ? `${target.lib}/${target.cell}` : "");
  useEffect(() => {
    setExcluded(new Set());
    setStale(false);
    if (!target) return;
    api.revision(project, target.lib, target.cell).then((r) => setPin({ revision: r.revision, key: selKey })).catch(() => setPin({ revision: null, key: selKey }));
  }, [selKey, project]); // eslint-disable-line

  const policy = !st ? "" : st.policy === "both" ? t("agent:policy.single") : st.policy === "none" ? t("agent:policy.none")
    : t("agent:policy.only", { kind: t(`agent:kind.${st.policy}`) });
  const mismatch = conv && st && (conv.connection.kind !== st.active.kind || conv.connection.model !== st.active.model);
  // the box may still be disabled while the proposal's cell loads: focus it once it can take text
  const usable = !!target && !!st?.ready && !mismatch;
  useEffect(() => {
    if (wantFocus.current && usable && input.current) { input.current.focus(); wantFocus.current = 0; }
  }, [focusNonce, usable]);

  const send = async () => {
    if (!target || !text.trim() || !st?.ready) return;
    const ctx: ChatCtx = { ...target, revision: pin.revision, selection: chips, overview };
    const message = text.trim();
    setBusy(true);
    try {
      const r = await api.chat(project, message, conv?.id ?? null, ctx, "propose", capture ? images : [], crypto.randomUUID());
      if (r.stale) { setStale(true); return; }
      setText("");
      setImages([]);
      setLive({ turn: r.turn!, events: [], state: r.state ?? "running" });
      setConv(await api.chatGet(project, r.conversation!));
      setPin((p) => ({ ...p, revision: r.revision ?? p.revision }));
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  // a turn that is still running (also after a reload): follow its progress until it ends
  useEffect(() => {
    if (live || !conv) return;
    const m = [...conv.messages].reverse().find((x) => x.role === "assistant" && x.turn && (x.state === "running" || x.state === "stopping"));
    if (m) setLive({ turn: m.turn!, events: [], state: m.state! });
  }, [conv, live]);
  const liveTurn = live?.turn;
  // the project changes when the view switches to the proposal: the polling goes on (the server maps it to the real one)
  const projectRef = useRef(project);
  projectRef.current = project;
  const cb = useRef({ onProposed, onTurnWork });
  cb.current = { onProposed, onTurnWork };
  useEffect(() => {
    if (!liveTurn) return;
    let gone = false, seen = 0;
    const inputs = new Map<string, { name: string; input?: Record<string, unknown> }>();
    const tick = async () => {
      try {
        const t = await api.turn(projectRef.current, liveTurn, seen);
        if (gone) return;
        seen += t.events.length;
        const targets: WorkTarget[] = [];
        let committed = false;
        for (const e of t.events) {
          if (e.type === "tool" && e.state === "running" && e.id) inputs.set(e.id, { name: e.name!, input: e.input });
          if (e.type === "tool" && e.gseq?.length) {
            committed = true;
            const i = inputs.get(e.id ?? "");
            const w = i && workTarget(i.name, i.input);
            if (w) targets.push(w);
          }
        }
        if (committed && t.changeset) cb.current.onTurnWork?.(liveTurn, t.changeset, targets);   // open, follow, refresh
        setLive((l) => (l && l.turn === liveTurn ? { ...l, events: [...l.events, ...t.events], state: t.state } : l));
        if (t.state !== "running" && t.state !== "stopping") {
          if (t.changeset) cb.current.onProposed?.();
          setConv(await api.chatGet(projectRef.current, t.conversation));
          setLive(null);
          return;
        }
      } catch { /* try again */ }
      if (!gone) timer = setTimeout(tick, 700);
    };
    let timer = setTimeout(tick, 300);
    return () => { gone = true; clearTimeout(timer); };
  }, [liveTurn]);

  const stopTurn = async () => {
    if (!live) return;
    try { const t = await api.turnStop(project, live.turn); setLive((l) => (l ? { ...l, state: t.state } : l)); }
    catch (e) { toast(apiErrorText(e), "error"); }
  };

  const refresh = async () => {
    if (!target) return;
    const r = await api.revision(project, target.lib, target.cell);
    setPin({ revision: r.revision, key: selKey });
    setStale(false);
  };

  const rebind = async (carry: boolean) => {
    if (!conv) return;
    if (carry && !(await confirmDialog({ title: msg("agent:rebind.title"), okLabel: msg("agent:rebind.confirm"),
      message: msg("agent:rebind.message", { connection: st?.label ?? "" }) }))) return;
    try { setConv(await api.chatRebind(project, conv.id, carry)); } catch (e) { toast(apiErrorText(e), "error"); }
  };


  return (
    <div className={docked ? "drawer chat docked" : "drawer chat"}>
      {!docked && <Splitter pane="drawer" className="drawer-edge" />}
      <div className="drawerhead">
        <Bot size={16} className="dim" />
        <b className="grow">{t("agent:title")}</b>
        <button className="icon" title={t("agent:connection")} onClick={() => setSettings((v) => !v)}><Settings2 size={15} /></button>
        <button className="icon" title={t("agent:close")} onClick={onClose}><X size={15} /></button>
      </div>

      <div className={`connbar ${st?.active.kind}`} onClick={() => setSettings(true)}>
        {st?.active.kind === "server" ? <Server size={13} /> : <Cloud size={13} />}
        <span title={st?.label}>{st?.label ?? "…"}</span>
        <span className="grow" />
        <span className="small dim" title={policy}>{policy}</span>
      </div>

      {settings && st && <ConnectionSettings st={st} onStatus={setSt} onClose={() => setSettings(false)} toast={toast} />}

      <div className="drawerbody chatbody">
        {!conv?.messages.length && (
          <div className="empty" style={{ padding: 0 }}>
            {t("agent:empty")}
          </div>
        )}
        {conv?.messages.map((m, i) => m.role === "assistant" && live && m.turn === live.turn
          ? <LiveTurn key={i} events={live.events} state={live.state} onStop={stopTurn} onReview={onReview} follow={follow}
            request={[...conv.messages].reverse().find((x) => x.role === "user" && x.turn === live.turn)?.text ?? ""} />
          : <Message key={i} m={m} onReview={onReview} project={project} />)}
        {busy && <div className="dim small"><span className="spinner" /> {t("agent:working")}</div>}
        {mismatch && (
          <div className="note warnnote">
            {t("agent:connectionMismatch", { previous: `${conv!.connection.kind} / ${conv!.connection.model}`, current: st!.label })}
            <div className="row" style={{ marginTop: 6 }}>
              <button className="ghost" onClick={() => rebind(false)}>{t("agent:startNew")}</button>
              <button className="ghost" onClick={() => rebind(true)}>{t("agent:continue")}</button>
            </div>
          </div>
        )}
        <div ref={end} />
      </div>

      <div className="composer">
        {stale && (
          <div className="note warnnote">{t("agent:stale")} <button className="link" onClick={refresh}>{t("agent:refresh")}</button></div>
        )}
        {capture && images.length > 0 && (
          <div className="thumbs composer">
            {images.map((im, i) => (
              <span key={i} className="thumb" title={t("agent:image.pending", { label: im.label })}>
                <img src={`data:${im.media_type};base64,${im.data}`} alt={im.label} />
                <span className="small dim">{im.label}</span>
                <button title={t("agent:image.remove", { label: im.label })} onClick={() => setImages(images.filter((_, j) => j !== i))}><X size={11} /></button>
              </span>
            ))}
          </div>
        )}
        <div className="chips">
          {chips.map((c) => (
            <span key={`${c.k}:${c.id ?? c.desc}`} className="attach">{c.label}
              <button title={t("agent:selection.remove", { label: c.label })} onClick={() => setExcluded(new Set([...excluded, `${c.k}:${c.id ?? c.desc}`]))}><X size={11} /></button></span>
          ))}
          {!chips.length && <span className="dim small">{target ? t("agent:selection.empty", { cell: `${target.lib}/${target.cell}` }) : t("agent:selection.open")}</span>}
          <span className="grow" />
          {capture && (
            <button className="ghost small" title={t("agent:image.attachHint")}
              disabled={images.length >= 2}
              onClick={() => { const im = capture(); if (im) setImages([...images, im]); else toast(msg("agent:image.empty"), "error"); }}>
              <ImagePlus size={13} /> {t("agent:image.attach")}
            </button>
          )}
          <label className="check small"><input type="checkbox" checked={overview} onChange={(e) => setOverview(e.target.checked)} /> {t("agent:context.overview")}</label>
        </div>
        <div className="row">
          <textarea ref={input} className="txt grow" rows={2} value={text}
            placeholder={!target ? t("agent:input.open") : project.includes("~") ? t("agent:input.proposal") : t("agent:input.design")}
            disabled={!usable}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); } }} />
          <button className="primary" title={t("agent:send")} disabled={busy || !!live || !text.trim() || !target || !st?.ready || !!mismatch} onClick={send}><Send size={14} /></button>
        </div>
        {st?.active.kind === "cloud" && !st.cloud_key_set && (
          <div className="small warn">{t("agent:missingKey")}</div>
        )}
        {st && !st.ready && <div className="small warn">{st.allowed.length ? t("agent:notReady") : t("agent:disabled")}</div>}
        <div className="small dim">{t("agent:sendShortcut")}</div>
      </div>
    </div>
  );
}
