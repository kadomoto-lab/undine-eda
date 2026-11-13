import { LOGO_BODY, LOGO_WAVE, LOGO_WORD } from "./logoMarkup";

/** The Undine logo. Colours follow the theme (--logo-body / --logo-wave in styles.css). */
export function Logo({ variant = "mark", height = 24, className }: { variant?: "mark" | "full"; height?: number; className?: string }) {
  const full = variant === "full";
  const vb = full ? "190 240 1400 400" : "225 275 380 320";
  const [w, h] = full ? [1400, 400] : [380, 320];
  return (
    <svg className={className} viewBox={vb} height={height} width={(height * w) / h} role="img" aria-label="Undine">
      <g fill="var(--logo-body)" dangerouslySetInnerHTML={{ __html: LOGO_BODY }} />
      <g fill="var(--logo-wave)" dangerouslySetInnerHTML={{ __html: LOGO_WAVE }} />
      {full && <g fill="var(--logo-body)" dangerouslySetInnerHTML={{ __html: LOGO_WORD }} />}
    </svg>
  );
}
