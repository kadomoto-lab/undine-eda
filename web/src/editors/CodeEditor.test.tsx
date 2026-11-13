// @vitest-environment jsdom
import { createRef } from "react";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { forEachDiagnostic } from "@codemirror/lint";
import { EditorView } from "@codemirror/view";
import { classHighlighter, highlightTree } from "@lezer/highlight";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale } from "../i18n";
import { CodeEditor, verilogLanguage, type CodeEditorHandle } from "./CodeEditor";

beforeAll(() => {   // jsdom has no layout; CodeMirror only needs these to exist
  Range.prototype.getClientRects = () => ({ length: 0, item: () => null, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
});
beforeEach(async () => { await setLocale("en"); });
afterEach(() => cleanup());

const SRC = "module inv(input logic a, output logic y);\n  // invert\n  assign y = ~a; $display(\"%d\", 8'hFF);\nendmodule\n";
const viewOf = () => EditorView.findFromDOM(document.querySelector(".cm-editor") as HTMLElement)!;

describe("the code editor", () => {
  it("colours SystemVerilog: keywords, types, comments, strings, numbers and system tasks apart", () => {
    const tree = verilogLanguage.parser.parse(SRC);
    const classes: Record<string, string> = {};
    highlightTree(tree, classHighlighter, (from, to, cls) => { classes[SRC.slice(from, to)] = cls; });
    expect(classes["module"]).toContain("tok-keyword");
    expect(classes["logic"]).toContain("tok-typeName");
    expect(classes["// invert"]).toContain("tok-comment");
    expect(classes['"%d"']).toContain("tok-string");
    expect(classes["8'hFF"]).toContain("tok-number");
    expect(classes["$display"]).not.toContain("tok-operator");
  });

  it("reports edits, takes new text from the server without reporting it, and saves on Ctrl+S", async () => {
    const onChange = vi.fn(), onSave = vi.fn();
    const { rerender } = render(<CodeEditor value={SRC} onChange={onChange} onSave={onSave} />);
    const v = viewOf();
    expect(v.state.doc.toString()).toBe(SRC);
    act(() => { v.dispatch({ changes: { from: 0, insert: "// top\n" } }); });
    expect(onChange).toHaveBeenLastCalledWith("// top\n" + SRC);
    onChange.mockClear();
    rerender(<CodeEditor value={"module x; endmodule\n"} onChange={onChange} onSave={onSave} />);
    expect(v.state.doc.toString()).toBe("module x; endmodule\n");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(v.contentDOM, { key: "s", ctrlKey: true });
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("draws lint messages in the text and goes to a line", () => {
    const ref = createRef<CodeEditorHandle>();
    render(<CodeEditor ref={ref} value={SRC} onChange={() => {}}
      diagnostics={[{ line: 3, col: 10, level: "warning", message: "unused" }, { line: 99, level: "error", message: "out of range" }]} />);
    const v = viewOf();
    const found: { from: number; to: number; severity: string }[] = [];
    forEachDiagnostic(v.state, (d, from, to) => found.push({ from, to, severity: d.severity }));
    const line3 = v.state.doc.line(3);
    expect(found).toEqual([{ from: line3.from + 9, to: line3.from + 10, severity: "warning" }]);
    act(() => ref.current!.goto(2));
    const line2 = v.state.doc.line(2);
    expect([v.state.selection.main.from, v.state.selection.main.to]).toEqual([line2.from, line2.to]);
  });

  it("is read-only when asked, and speaks the UI language in its own panels", async () => {
    const { rerender } = render(<CodeEditor value={SRC} onChange={() => {}} readOnly />);
    const v = viewOf();
    expect(v.state.readOnly).toBe(true);
    rerender(<CodeEditor value={SRC} onChange={() => {}} />);
    expect(v.state.readOnly).toBe(false);
    expect(v.state.phrase("Find")).toBe("Find");
    await act(async () => { await setLocale("ja"); });
    expect(v.state.phrase("Find")).toBe("検索");
    await act(async () => { await setLocale("en"); });
  });
});
