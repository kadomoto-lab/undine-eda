// Catch newly hard-coded UI text. Design identifiers, units and keyboard keys are intentional.
import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

const allowed = new Set([
  "Undine", "English", "日本語", "PDK", "DRC", "LVS", "GDS", "OASIS", "SPICE", "ngspice", "Netgen", "Magic", "KLayout", "Xschem",
  "F", "R", "P", "M", "C", "I", "K", "U", "S", "Shift", "Ctrl", "Alt", "Esc", "Enter",
  "X", "Y", "W", "H", "μm", "µm", "nm", "deg", "°", "dB", "Hz", "V", "A", "x", "y", "w", "h", "s",
  "DBU", "PCell", "vlsirtools", "Xyce", "Spectre", "https://…/v1", "~/netlist.spice",
  "1x2 (161 × 225.76 µm)", "2x2 (334.88 × 225.76 µm)",
]);
const findings = [];
function check(file) {
  const source = ts.createSourceFile(file, fs.readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  function visit(node) {
    let text;
    if (ts.isJsxText(node)) text = node.text.trim().replace(/\s+/g, " ");
    if (ts.isJsxAttribute(node) && ["title", "placeholder", "aria-label", "alt"].includes(node.name.getText(source)) && node.initializer && ts.isStringLiteral(node.initializer)) text = node.initializer.text;
    if (text && /\p{L}/u.test(text) && !allowed.has(text)) {
      const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
      findings.push(`${file}:${line + 1}: ${text}`);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
}
function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(file);
    else if (file.endsWith(".tsx") && !file.endsWith(".test.tsx")) check(file);
  }
}
walk("src");
if (findings.length) { console.error("Move UI text to a semantic translation key:\n" + findings.join("\n")); process.exitCode = 1; }
else console.log("UI text check passed.");
