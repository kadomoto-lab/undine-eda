// A code editor (CodeMirror 6) for the RTL view: Verilog / SystemVerilog highlighting, line numbers, folding, bracket
// matching, search, in-editor undo, and lint diagnostics drawn in the text. The document is controlled: `value` from
// the server replaces it (a load, a save, a server undo), and edits come back through `onChange`. Colours come from the
// palette in styles.css, so the editor sits in the same water as the rest of the UI.
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { autocompletion, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { bracketMatching, foldGutter, foldKeymap, HighlightStyle, indentOnInput, indentUnit, StreamLanguage, syntaxHighlighting,
  type StreamParser } from "@codemirror/language";
import { verilog as verilogMode } from "@codemirror/legacy-modes/mode/verilog";
import { lintGutter, setDiagnostics, type Diagnostic } from "@codemirror/lint";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { Annotation, Compartment, EditorSelection, EditorState, Prec } from "@codemirror/state";
import { drawSelection, EditorView, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars, keymap, lineNumbers }
  from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { t, useI18n } from "../i18n";

// The legacy Verilog mode calls operators and system tasks both "meta", and macros and delays both "def"; types are
// keywords. Split them so each gets its own colour.
const TYPES = new Set(("logic wire reg bit byte int integer shortint longint real shortreal realtime time tri tri0 tri1 " +
  "triand trior wand wor uwire supply0 supply1 signed unsigned genvar string event chandle void").split(" "));
const base = verilogMode as StreamParser<unknown>;
const systemVerilog: StreamParser<unknown> = {
  ...base,
  name: "systemverilog",
  token(stream, state) {
    const style = base.token(stream, state);
    const word = stream.current();
    if (style === "meta") return word.startsWith("$") ? "systemTask" : "operator";
    if (style === "def") return word.startsWith("`") ? "macro" : "number";
    if (style === "keyword" && TYPES.has(word)) return "type";
    return style;
  },
  tokenTable: { systemTask: tags.special(tags.variableName), operator: tags.operator, macro: tags.processingInstruction, type: tags.typeName },
};
export const verilogLanguage = StreamLanguage.define(systemVerilog);

const highlight = HighlightStyle.define([
  { tag: tags.keyword, color: "var(--accent-hi)" },
  { tag: tags.typeName, color: "var(--probe)" },
  { tag: tags.string, color: "var(--warn)" },
  { tag: tags.number, color: "var(--apricot)" },
  { tag: [tags.special(tags.variableName), tags.processingInstruction], color: "var(--ok)" },
  { tag: tags.comment, color: "var(--faint)", fontStyle: "italic" },
  { tag: tags.operator, color: "var(--text-soft)" },
  { tag: tags.bracket, color: "var(--dim)" },
]);

// the lint gutter's markers, in the palette's coral and sand (CSS variables cannot reach into a data URL)
const marker = (svg: string) => `url('data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">${svg}</svg>`)}')`;
const theme = EditorView.theme({
  "&": { height: "100%", backgroundColor: "var(--bg0)", color: "var(--text)", fontSize: "12.5px" },
  // no ligatures: in Verilog `<=` is a non-blocking assignment, not "≤", and `!=` / `===` must read as typed
  ".cm-scroller": { fontFamily: "var(--mono)", lineHeight: "18px", fontVariantLigatures: "none" },
  ".cm-content": { padding: "8px 0", caretColor: "var(--accent)" },
  "&.cm-focused": { outline: "none" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "var(--accent)" },
  ".cm-gutters": { backgroundColor: "var(--bg0)", color: "var(--faint)", border: "none" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 6px 0 10px" },
  ".cm-activeLine": { backgroundColor: "rgba(var(--accent-rgb), 0.05)" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text-soft)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection":
    { backgroundColor: "rgba(var(--sel-rgb), 0.22)" },
  ".cm-selectionMatch": { backgroundColor: "rgba(var(--sel-rgb), 0.12)" },
  "&.cm-focused .cm-matchingBracket": { backgroundColor: "rgba(var(--accent-rgb), 0.2)", outline: "none" },
  "&.cm-focused .cm-nonmatchingBracket": { backgroundColor: "rgba(var(--err-rgb), 0.25)" },
  ".cm-searchMatch": { backgroundColor: "rgba(var(--sel-rgb), 0.18)", outline: "1px solid rgba(var(--sel-rgb), 0.4)" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "rgba(var(--sel-rgb), 0.35)" },
  ".cm-foldPlaceholder": { backgroundColor: "var(--bg2)", border: "1px solid var(--line2)", color: "var(--dim)", borderRadius: "var(--radius)" },
  ".cm-foldGutter .cm-gutterElement": { color: "var(--faint)", padding: "0 4px" },
  ".cm-lintRange-error": { backgroundImage: "none", textDecoration: "underline wavy var(--err)", textUnderlineOffset: "3px" },
  ".cm-lintRange-warning": { backgroundImage: "none", textDecoration: "underline wavy var(--warn)", textUnderlineOffset: "3px" },
  ".cm-lint-marker-error": { content: marker('<circle cx="20" cy="20" r="11" fill="#e9929a"/>') },
  ".cm-lint-marker-warning": { content: marker('<path d="M20 7 L34 32 L6 32 Z" fill="#e6c88f"/>') },
  ".cm-tooltip": { backgroundColor: "var(--bg1)", color: "var(--text)", border: "1px solid var(--line2)", borderRadius: "var(--radius)" },
  ".cm-diagnostic": { borderLeft: "none", padding: "4px 10px", fontFamily: "var(--sans)" },
  ".cm-diagnostic-error": { color: "var(--err-soft)" },
  ".cm-diagnostic-warning": { color: "var(--warn-soft)" },
  ".cm-panels": { backgroundColor: "var(--bg1)", color: "var(--text)" },
  ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--line)" },
}, { dark: true });

/** A lint message: 1-based line and column, as Verilator reports them. */
export interface CodeDiagnostic { line: number; col?: number | null; level: "error" | "warning"; message: string; source?: string }
export interface CodeEditorHandle { goto(line: number, col?: number | null): void }

const fromServer = Annotation.define<boolean>();

// CodeMirror's own words (search panel, folding, lint), keyed by its English phrase
const PHRASES = {
  "Find": "find", "Replace": "replace", "next": "next", "previous": "previous", "all": "all", "match case": "matchCase",
  "by word": "byWord", "regexp": "regexp", "replace": "replaceOne", "replace all": "replaceAll", "close": "close",
  "current match": "currentMatch", "on line": "onLine", "Go to line": "gotoLine", "go": "go", "folded code": "foldedCode",
  "unfold": "unfold", "to": "to", "Fold line": "foldLine", "Unfold line": "unfoldLine", "Folded lines": "foldedLines",
  "Unfolded lines": "unfoldedLines", "Diagnostics": "diagnostics", "No diagnostics": "noDiagnostics", "Selection deleted": "selectionDeleted",
} as const;
const phrases = () => EditorState.phrases.of(Object.fromEntries(Object.entries(PHRASES).map(([en, k]) => [en, t(`rtl:cm.${k}`)])));

export const CodeEditor = forwardRef<CodeEditorHandle, {
  value: string; onChange: (text: string) => void; readOnly?: boolean; diagnostics?: CodeDiagnostic[]; onSave?: () => void;
  label?: string;
}>(function CodeEditor({ value, onChange, readOnly = false, diagnostics = [], onSave, label }, ref) {
  const { locale } = useI18n();
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const handlers = useRef({ onChange, onSave });
  handlers.current = { onChange, onSave };
  const comp = useRef({ editable: new Compartment(), history: new Compartment(), phrases: new Compartment() });

  useEffect(() => {
    const c = comp.current;
    const v = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          Prec.highest(keymap.of([{ key: "Mod-s", preventDefault: true, run: () => { handlers.current.onSave?.(); return true; } }])),
          lineNumbers(), foldGutter(), lintGutter(), highlightActiveLineGutter(), highlightSpecialChars(), drawSelection(),
          c.phrases.of(phrases()), c.history.of(history()), indentUnit.of("  "), EditorState.tabSize.of(2), indentOnInput(), bracketMatching(), closeBrackets(),
          autocompletion({ activateOnTyping: false }), highlightActiveLine(), highlightSelectionMatches(), search({ top: false }),
          verilogLanguage, syntaxHighlighting(highlight), theme,
          keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...searchKeymap, ...historyKeymap, ...foldKeymap, indentWithTab]),
          c.editable.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
          EditorView.contentAttributes.of({ "aria-label": label ?? "", spellcheck: "false" }),
          EditorView.updateListener.of((u) => {
            if (u.docChanged && !u.transactions.some((tr) => tr.annotation(fromServer))) handlers.current.onChange(u.state.doc.toString());
          }),
        ],
      }),
    });
    view.current = v;
    return () => { v.destroy(); view.current = null; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- one view for the life of the component; props flow in below

  // a new text from the server replaces the document, and the in-editor undo starts over from it
  useEffect(() => {
    const v = view.current;
    if (!v || v.state.doc.toString() === value) return;
    v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value }, annotations: fromServer.of(true),
      effects: comp.current.history.reconfigure([]) });
    v.dispatch({ effects: comp.current.history.reconfigure(history()) });
  }, [value]);

  useEffect(() => { view.current?.dispatch({ effects: comp.current.phrases.reconfigure(phrases()) }); }, [locale]);

  useEffect(() => {
    view.current?.dispatch({ effects: comp.current.editable.reconfigure([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]) });
  }, [readOnly]);

  useEffect(() => {
    const v = view.current;
    if (!v) return;
    const doc = v.state.doc;
    const ds: Diagnostic[] = diagnostics.filter((d) => d.line >= 1 && d.line <= doc.lines).map((d) => {
      const line = doc.line(d.line);
      const from = Math.min(line.to, line.from + Math.max(0, (d.col ?? 1) - 1));
      const word = v.state.wordAt(from);
      const to = word && word.from === from ? word.to : d.col ? Math.min(line.to, from + 1) : line.to;
      return { from, to: Math.max(to, from), severity: d.level, message: d.message, source: d.source };
    });
    v.dispatch(setDiagnostics(v.state, ds));
  }, [diagnostics]); // set when a lint result arrives; CodeMirror maps them through later edits

  useImperativeHandle(ref, () => ({
    goto(lineNo, col) {
      const v = view.current;
      if (!v || lineNo < 1 || lineNo > v.state.doc.lines) return;
      const line = v.state.doc.line(lineNo);
      const at = Math.min(line.to, line.from + Math.max(0, (col ?? 1) - 1));
      v.dispatch({ selection: col ? EditorSelection.cursor(at) : EditorSelection.range(line.from, line.to),
        effects: EditorView.scrollIntoView(at, { y: "center" }) });
      v.focus();
    },
  }), []);

  return <div className="codeeditor" ref={host} />;
});
