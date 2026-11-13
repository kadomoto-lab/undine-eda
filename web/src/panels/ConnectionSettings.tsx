// The assistant's connection: the built-in ones (keys in the server's environment) and connections registered here with
// their own API key (server/undine/agents/llm.py: Connections). A key is sent once and never shown again, only its last 4 characters.
import { useState } from "react";
import { Plus, RefreshCw, Trash2 } from "lucide-react";
import { api, LlmConnection, LlmStatus } from "../api";
import { confirmDialog } from "../dialog";
import { apiErrorText, msg, t, useI18n, type UiText } from "../i18n";

interface Props {
  st: LlmStatus;
  onStatus: (s: LlmStatus) => void;
  onClose: () => void;
  toast: (m: UiText, k?: "error" | "info") => void;
}

// Stable default connection names are persisted on the server; only the option's UI label is translated.
const PROVIDERS = [
  { value: "anthropic", label: "Anthropic (Claude)", url: "" },
  { value: "openai", label: "OpenAI", url: "" },
  { value: "openai-compatible", label: "OpenAI-compatible (other URL)", url: "https://" },
] as const;

const where = (c: LlmConnection) => t(c.where === "cloud" ? "agent:connection.whereCloud" : "agent:connection.whereServer", { url: c.base_url });

export function ConnectionSettings({ st, onStatus, onClose, toast }: Props) {
  useI18n();
  const a = st.active;
  const [choice, setChoice] = useState<string>(a.connection ?? `builtin-${a.kind}`);
  const [model, setModel] = useState(a.model);
  const [baseUrl, setBaseUrl] = useState(a.connection ? st.server_url_default : a.base_url ?? st.server_url_default);
  const [adding, setAdding] = useState(!st.connections.length && !st.cloud_key_set);
  const [add, setAdd] = useState({ provider: "anthropic", label: "", api_key: "", base_url: "" });
  const [busy, setBusy] = useState(false);
  const conn = st.connections.find((c) => c.id === choice);

  const pick = (id: string) => {
    setChoice(id);
    const c = st.connections.find((x) => x.id === id);
    setModel(c ? (id === a.connection ? a.model : c.models[0] ?? "") : id === "builtin-cloud" ? "claude-haiku-5-5" : "");
  };

  const use = async () => {
    setBusy(true);
    try {
      const s = conn ? await api.llmUse(conn.id, model || null)
        : await api.llmSwitch(choice === "builtin-cloud" ? "cloud" : "server", model || null, choice === "builtin-server" ? baseUrl : null);
      onStatus(s);
      onClose();
      toast(msg("agent:connection.changed", { label: s.label }));
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  const save = async () => {
    setBusy(true);
    try {
      const provider = add.provider === "openai-compatible" ? "openai" : add.provider;
      const r = await api.llmAdd({ label: add.label || PROVIDERS.find((p) => p.value === add.provider)!.label, provider,
        api_key: add.api_key, base_url: add.base_url || null });
      onStatus(r);
      setAdding(false);
      setAdd({ provider: "anthropic", label: "", api_key: "", base_url: "" });
      pick(r.added);
      setModel(r.available_models[0] ?? "");
      toast(msg("agent:connection.added", { count: r.available_models.length }));
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  const remove = async (c: LlmConnection) => {
    if (!(await confirmDialog({ title: msg("agent:connection.removeTitle", { label: c.label }), okLabel: msg("agent:connection.remove"), danger: true,
      message: msg("agent:connection.removeMessage") }))) return;
    try { const s = await api.llmRemove(c.id); onStatus(s); if (choice === c.id) pick(`builtin-${s.active.kind}`); }
    catch (e) { toast(apiErrorText(e), "error"); }
  };

  const refresh = async (c: LlmConnection) => {
    try { onStatus(await api.llmRefreshModels(c.id)); toast(msg("agent:connection.modelsUpdated")); }
    catch (e) { toast(apiErrorText(e), "error"); }
  };

  const allowed = (w: "cloud" | "server") => st.allowed.includes(w);
  return (
    <div className="connform">
      <div className="conns">
        {allowed("cloud") && (
          <label className="connrow"><input type="radio" checked={choice === "builtin-cloud"} onChange={() => pick("builtin-cloud")} />
            <span className="grow">{t("agent:connection.builtinCloud")} <span className="dim small">{t(st.cloud_key_set ? "agent:connection.keySet" : "agent:connection.keyMissing")}</span></span>
          </label>
        )}
        {allowed("server") && (
          <label className="connrow"><input type="radio" checked={choice === "builtin-server"} onChange={() => pick("builtin-server")} />
            <span className="grow">{t("agent:connection.builtinServer")} <span className="dim small">{t("agent:connection.compatibleHint")}</span></span>
          </label>
        )}
        {st.connections.map((c) => (
          <label key={c.id} className={`connrow ${allowed(c.where) ? "" : "dim"}`} title={allowed(c.where) ? "" : t("agent:connection.blocked", { kind: t(`agent:kind.${c.where}`) })}>
            <input type="radio" disabled={!allowed(c.where)} checked={choice === c.id} onChange={() => pick(c.id)} />
            <span className="grow ellip">{c.label} <span className="dim small">{t("agent:connection.summary", { provider: c.provider, hint: c.key_hint ?? t("agent:connection.noKey"), count: c.models.length })}</span></span>
            <button className="icon" title={t("agent:connection.refreshHint")} onClick={(e) => { e.preventDefault(); refresh(c); }}><RefreshCw size={12} /></button>
            <button className="icon" title={t("agent:connection.removeHint")} onClick={(e) => { e.preventDefault(); remove(c); }}><Trash2 size={12} /></button>
          </label>
        ))}
      </div>
      <label><span>{t("agent:connection.model")}</span>
        <input className="txt" list="llm-models" value={model} onChange={(e) => setModel(e.target.value)}
          placeholder={choice === "builtin-cloud" ? "claude-haiku-5-5" : t("agent:connection.modelPlaceholder")} />
        <datalist id="llm-models">{(conn?.models ?? []).map((m) => <option key={m} value={m} />)}</datalist>
      </label>
      {choice === "builtin-server" && <label><span>{t("agent:connection.endpoint")}</span><input className="txt" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} /></label>}
      <div className="note small">
        {conn ? where(conn)
          : choice === "builtin-cloud" ? t("agent:connection.whereBuiltinCloud")
          : t("agent:connection.whereBuiltinServer")}
        {" "}{t("agent:connection.noFallback")}
      </div>
      <div className="row">
        <button className="ghost" onClick={() => setAdding((v) => !v)}><Plus size={12} /> {t("agent:connection.add")}</button>
        <span className="grow" />
        <button className="ghost" onClick={onClose}>{t("agent:connection.cancel")}</button>
        <button className="primary" disabled={busy || (!model && choice !== "builtin-cloud")} onClick={use}>{t("agent:connection.use")}</button>
      </div>
      {adding && (
        <div className="connadd">
          <label><span>{t("agent:connection.provider")}</span>
            <select value={add.provider} onChange={(e) => setAdd({ ...add, provider: e.target.value, base_url: PROVIDERS.find((p) => p.value === e.target.value)!.url })}>
              {PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.value === "openai-compatible" ? t("agent:connection.compatible") : p.label}</option>)}
            </select></label>
          <label><span>{t("agent:connection.name")}</span><input className="txt" value={add.label} placeholder={t("agent:connection.namePlaceholder")} onChange={(e) => setAdd({ ...add, label: e.target.value })} /></label>
          <label><span>{t("agent:connection.apiKey")}</span><input className="txt" type="password" autoComplete="off" value={add.api_key} onChange={(e) => setAdd({ ...add, api_key: e.target.value })} /></label>
          {add.provider === "openai-compatible" && <label><span>{t("agent:connection.baseUrl")}</span><input className="txt" value={add.base_url} placeholder="https://…/v1" onChange={(e) => setAdd({ ...add, base_url: e.target.value })} /></label>}
          <div className="note small">{t("agent:connection.keyNote")}</div>
          <div className="row"><span className="grow" />
            <button className="primary" disabled={busy || (!add.api_key && add.provider !== "openai-compatible")} onClick={save}>{t("agent:connection.save")}</button></div>
        </div>
      )}
    </div>
  );
}
