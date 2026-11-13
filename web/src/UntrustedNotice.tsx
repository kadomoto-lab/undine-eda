// A launch refused because a dependency's content is not trusted (403, code untrusted: server/undine/store/trust.py
// UntrustedContent): which cell, from which dependency, the lines the trust rule refuses, and Trust... for a person
// (TrustDialog). The panels that launch runs (simulation, LVS, RTL, the Xschem netlist) show it for that error.
import { useState } from "react";
import { ShieldAlert } from "lucide-react";
import { untrustedOf } from "./api";
import { FindingList, TrustDialog } from "./TrustDialog";
import { useI18n } from "./i18n";

export function UntrustedNotice({ project, error, onTrusted }: {
  project: string;
  error: unknown;                  // anything: renders nothing unless it is an 'untrusted' ApiError
  onTrusted?: () => void | Promise<void>;     // trusted from the dialog: run it again
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const r = untrustedOf(error);
  if (!r) return null;
  const label = r.package ? r.key.replace("@", " ") : r.name;
  // a changeset preview (p~cid): trust is the project's, and the route refuses it here; it reaches the preview
  const preview = project.includes("~");
  return (
    <div className="note warnnote" data-testid="untrusted-notice">
      <div><ShieldAlert size={13} /> {t("app:untrusted.refused", { cell: r.cell || r.name, label })}</div>
      {r.findings.length ? <FindingList findings={r.findings} /> : <div className="small" style={{ whiteSpace: "pre-line" }}>{r.detail}</div>}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }}>
        {r.name && !preview && <button type="button" className="ghost" onClick={() => setOpen(true)}>{t("app:untrusted.trust")}</button>}
        <span className="faint small">{preview ? t("app:untrusted.inPreview", { name: label }) : t("app:untrusted.agent")}</span>
      </div>
      {open && !preview && <TrustDialog project={project} name={r.name} onClose={() => setOpen(false)}
        onChanged={async (trusted) => { if (trusted) await onTrusted?.(); }} />}
    </div>
  );
}
