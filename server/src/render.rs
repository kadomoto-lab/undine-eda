//! render(): one schematic / symbol view flattened to drawing primitives, as undine/xschem.py `_py_render`
//! produced them (the tests compare the two). Symbols are read once per process and re-read when their
//! file changes (mtime, size, inode).

use std::collections::HashMap;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::model::{self, Props, XComp, XFile, XText};

pub const FONT_SCALE: f64 = 52.0;
pub const TEXT_LAYER: i64 = 3;
pub const PIN_LAYER: i64 = 5;

// ----------------------------------------------------------------------------- symbol files

type Stamp = (i64, i64, u64, u64); // mtime s, mtime ns, size, inode

fn cache() -> &'static Mutex<HashMap<PathBuf, (Stamp, Arc<XFile>)>> {
    static C: OnceLock<Mutex<HashMap<PathBuf, (Stamp, Arc<XFile>)>>> = OnceLock::new();
    C.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn read_lossy(p: &Path) -> Option<String> {
    std::fs::read(p).ok().map(|b| String::from_utf8_lossy(&b).into_owned())
}

/// A symbol (or embedded schematic) file, parsed once while it does not change.
pub fn load_file(p: &Path) -> Option<Arc<XFile>> {
    let m = std::fs::metadata(p).ok()?;
    let stamp = (m.mtime(), m.mtime_nsec(), m.size(), m.ino());
    if let Some((s, f)) = cache().lock().unwrap().get(p) {
        if *s == stamp {
            return Some(f.clone());
        }
    }
    let f = Arc::new(model::parse(&read_lossy(p)?));
    cache().lock().unwrap().insert(p.to_path_buf(), (stamp, f.clone()));
    Some(f)
}

/// The SymbolResolver of xschem.py: absolute name, next to the schematic, then each search path; a name
/// without .sym / .sch is also tried with .sym appended.
pub struct Resolver {
    paths: Vec<PathBuf>,
    memo: HashMap<(Option<PathBuf>, String), Option<Arc<XFile>>>,
}

impl Resolver {
    pub fn new(paths: &[String]) -> Self {
        Resolver { paths: paths.iter().map(PathBuf::from).filter(|p| p.is_dir()).collect(), memo: HashMap::new() }
    }

    pub fn find(&self, name: &str, base: Option<&Path>) -> Option<PathBuf> {
        let mut cands: Vec<PathBuf> = Vec::new();
        if Path::new(name).is_absolute() {
            cands.push(PathBuf::from(name));
        }
        if let Some(b) = base {
            cands.push(b.join(name));
        }
        for p in &self.paths {
            cands.push(p.join(name));
        }
        if !(name.ends_with(".sym") || name.ends_with(".sch")) {
            let extra: Vec<PathBuf> = cands
                .iter()
                .filter_map(|c| c.file_name().map(|n| {
                    let mut s = n.to_os_string();
                    s.push(".sym");
                    c.with_file_name(s)
                }))
                .collect();
            cands.extend(extra);
        }
        cands.into_iter().find(|c| c.is_file())
    }

    pub fn load(&mut self, name: &str, base: Option<&Path>) -> Option<Arc<XFile>> {
        let key = (base.map(Path::to_path_buf), name.to_string());
        if let Some(v) = self.memo.get(&key) {
            return v.clone();
        }
        let v = self.find(name, base).and_then(|p| load_file(&p));
        self.memo.insert(key, v.clone());
        v
    }
}

// ----------------------------------------------------------------------------- helpers

/// Python's Path(name).stem.
pub fn py_stem(name: &str) -> &str {
    let last = name.trim_end_matches('/').rsplit('/').next().unwrap_or("");
    match last.rfind('.') {
        Some(i) if i > 0 && i + 1 < last.len() => &last[..i],
        _ => last,
    }
}

/// Python's "{:g}".format(v).
pub fn py_g(v: f64) -> String {
    py_gp(v, 6)
}

/// Python's format(v, ".<prec>g").
pub fn py_gp(v: f64, prec: usize) -> String {
    if v.is_nan() {
        return "nan".into();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if v == 0.0 {
        return if v.is_sign_negative() { "-0".into() } else { "0".into() };
    }
    let prec = prec.max(1);
    let e = format!("{:.*e}", prec - 1, v); // mantissa with `prec` significant digits, correctly rounded
    let (mant, exp) = e.split_once('e').unwrap();
    let exp: i32 = exp.parse().unwrap();
    let strip = |s: &str| -> String {
        if s.contains('.') { s.trim_end_matches('0').trim_end_matches('.').to_string() } else { s.to_string() }
    };
    if exp < -4 || exp >= prec as i32 {
        format!("{}e{}{:02}", strip(mant), if exp < 0 { '-' } else { '+' }, exp.abs())
    } else {
        strip(&format!("{:.*}", (prec as i32 - 1 - exp).max(0) as usize, v))
    }
}

pub fn wire_id(x1: f64, y1: f64, x2: f64, y2: f64) -> String {
    let (a, b) = if (x1, y1) <= (x2, y2) { ((x1, y1), (x2, y2)) } else { ((x2, y2), (x1, y1)) };
    format!("{},{},{},{}", py_g(a.0), py_g(a.1), py_g(b.0), py_g(b.1))
}

pub fn comp_ids(f: &XFile) -> Vec<String> {
    let mut seen: HashMap<String, usize> = HashMap::new();
    f.comps
        .iter()
        .enumerate()
        .map(|(i, c)| {
            let base = match c.props.get("name") {
                Some(n) if !n.is_empty() => n.to_string(),
                _ => format!("@{i}"),
            };
            let k = seen.entry(base.clone()).or_insert(0);
            *k += 1;
            if *k == 1 { base } else { format!("{base}#{k}") }
        })
        .collect()
}

/// Xschem ROTATION() around the origin.
#[inline]
pub fn rot(r: i64, flip: i64, x: f64, y: f64) -> (f64, f64) {
    let x = if flip != 0 { -x } else { x };
    match r {
        0 => (x, y),
        1 => (-y, x),
        2 => (-x, -y),
        _ => (y, -x),
    }
}

fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// tcleval(...) unwrapped (greedy, as re.sub(r"tcleval\((.*)\)", r"\1", s, flags=re.S)), then @tokens replaced.
pub fn subst(s: &str, inst: &Props, tmpl: &Props, symname: &str) -> String {
    let unwrapped;
    let s = match (s.find("tcleval("), s.rfind(')')) {
        (Some(a), Some(b)) if b >= a + 8 => {
            unwrapped = format!("{}{}{}", &s[..a], &s[a + 8..b], &s[b + 1..]);
            unwrapped.as_str()
        }
        _ => s,
    };
    let b = s.as_bytes();
    let n = b.len();
    let mut out = String::with_capacity(n);
    let mut i = 0;
    let mut last = 0;
    let word = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let alpha = |c: u8| c.is_ascii_alphabetic() || c == b'_';
    while i < n {
        if b[i] != b'@' {
            i += 1;
            continue;
        }
        // @#pin:attr
        let mut end = None;
        let mut key: Option<&str> = None;
        if i + 1 < n && b[i + 1] == b'#' {
            let mut j = i + 2;
            while j < n && word(b[j]) {
                j += 1;
            }
            if j > i + 2 && j < n && b[j] == b':' {
                let mut k = j + 1;
                while k < n && alpha(b[k]) {
                    k += 1;
                }
                if k > j + 1 {
                    end = Some(k);
                    key = Some(&s[i + 1..k]);
                }
            }
        }
        // @name
        if end.is_none() && i + 1 < n && alpha(b[i + 1]) {
            let mut j = i + 2;
            while j < n && word(b[j]) {
                j += 1;
            }
            end = Some(j);
            key = Some(&s[i + 1..j]);
        }
        match (end, key) {
            (Some(e), Some(k)) => {
                out.push_str(&s[last..i]);
                if !k.starts_with('#') {
                    if k == "symname" {
                        out.push_str(symname);
                    } else if let Some(v) = inst.get(k) {
                        out.push_str(v);
                    } else if let Some(v) = tmpl.get(k) {
                        out.push_str(v);
                    }
                }
                i = e;
                last = e;
            }
            _ => i += 1,
        }
    }
    out.push_str(&s[last..]);
    out
}

fn fill(p: &Props) -> i64 {
    match p.get_or("fill", "") {
        "full" => 2,
        "true" => 1,
        _ => 0,
    }
}

/// Python's int() of an attribute: surrounding whitespace allowed, otherwise the same ValueError.
fn py_int(v: &str) -> PyResult<i64> {
    let t = v.trim_matches(py_isspace);
    let ok = t.replace('_', "");
    ok.parse::<i64>()
        .ok()
        .filter(|_| !t.starts_with('_') && !t.ends_with('_') && !t.contains("__"))
        .ok_or_else(|| PyValueError::new_err(format!("invalid literal for int() with base 10: '{v}'")))
}

// ----------------------------------------------------------------------------- primitives

pub struct Text {
    pub s: String,
    pub x: f64,
    pub y: f64,
    pub rot: i64,
    pub flip: i64,
    pub size: f64,
    pub layer: i64,
    pub hc: bool,
    pub vc: bool,
    pub bold: bool,
    pub italic: bool,
}

#[derive(Default)]
pub struct Prims {
    pub lines: Vec<(i64, f64, f64, f64, f64)>,
    pub boxes: Vec<(i64, f64, f64, f64, f64, i64)>,
    pub polys: Vec<(i64, i64, Vec<f64>)>,
    pub arcs: Vec<(i64, f64, f64, f64, f64, f64, i64)>,
    pub texts: Vec<Text>,
}

/// Approximate text extent as web/src/editors/schdraw.ts textBox() draws it (Inter: ~0.55 em per character).
pub fn text_box(t: &Text) -> [f64; 4] {
    let lines: Vec<&str> = t.s.split('\n').collect();
    let w = lines.iter().map(|l| l.chars().count()).max().unwrap_or(0) as f64 * t.size * 0.55;
    let h = lines.len() as f64 * t.size * 1.16;
    let (mut x1, mut y1) = (t.x, t.y);
    let (r, f) = (t.rot & 3, t.flip != 0);
    if t.hc {
        let d = w / 2.0;
        match r {
            0 => x1 += if f { d } else { -d },
            1 => y1 += if f { d } else { -d },
            2 => x1 += if f { -d } else { d },
            _ => y1 += if f { -d } else { d },
        }
    }
    if t.vc {
        let d = h / 2.0;
        match r {
            0 => y1 -= d,
            1 => x1 += d,
            2 => y1 += d,
            _ => x1 -= d,
        }
    }
    let (px, py) = (x1 + w, y1 + h);
    let xx = if f { 2.0 * t.x - px } else { px };
    let (rx, ry) = match r {
        0 => (xx, py),
        1 => (t.x - py + t.y, t.y + xx - t.x),
        2 => (2.0 * t.x - xx, 2.0 * t.y - py),
        _ => (t.x + py - t.y, t.y - xx + t.x),
    };
    [x1.min(rx), y1.min(ry), x1.max(rx), y1.max(ry)]
}

fn emit_text(out: &mut Prims, t: &XText, text: String, x: f64, y: f64, rot: i64, flip: i64) -> PyResult<()> {
    if t.props.get_or("hide", "") == "true" || text.trim_matches(py_isspace).is_empty() {
        return Ok(());
    }
    let layer = match t.props.get("layer") {
        Some(v) if !v.is_empty() => py_int(v)?,
        _ => TEXT_LAYER,
    };
    out.texts.push(Text {
        s: text,
        x,
        y,
        rot,
        flip,
        size: t.xs * FONT_SCALE,
        layer,
        hc: t.props.get_or("hcenter", "") == "true",
        vc: t.props.get_or("vcenter", "") == "true",
        bold: t.props.get_or("weight", "") == "bold",
        italic: matches!(t.props.get_or("slant", ""), "italic" | "oblique"),
    });
    Ok(())
}

pub fn emit_symbol(out: &mut Prims, sym: &XFile, comp: &XComp, symname: &str) -> PyResult<[f64; 4]> {
    let (r, flip, ox, oy) = (comp.rot, comp.flip, comp.x, comp.y);
    let tmpl = model::parse_props(sym.global_props.get_or("template", ""));
    let mut bb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    let tp = |x: f64, y: f64, bb: &mut [f64; 4]| -> (f64, f64) {
        let (rx, ry) = rot(r, flip, x, y);
        let (rx, ry) = (rx + ox, ry + oy);
        bb[0] = bb[0].min(rx);
        bb[1] = bb[1].min(ry);
        bb[2] = bb[2].max(rx);
        bb[3] = bb[3].max(ry);
        (rx, ry)
    };
    for (layer, x1, y1, x2, y2, _) in &sym.lines {
        let (a, b) = (tp(*x1, *y1, &mut bb), tp(*x2, *y2, &mut bb));
        out.lines.push((*layer, a.0, a.1, b.0, b.1));
    }
    for (layer, x1, y1, x2, y2, props) in &sym.boxes {
        if props.get_or("flags", "").starts_with("graph") {
            continue;
        }
        let (a, b) = (tp(*x1, *y1, &mut bb), tp(*x2, *y2, &mut bb));
        out.boxes.push((*layer, a.0.min(b.0), a.1.min(b.1), a.0.max(b.0), a.1.max(b.1),
                        if *layer != PIN_LAYER { fill(props) } else { 1 }));
    }
    for (layer, pts, props) in &sym.polys {
        let mut q = Vec::with_capacity(pts.len());
        let mut k = 0;
        while k + 1 < pts.len() {
            let (x, y) = tp(pts[k], pts[k + 1], &mut bb);
            q.push(x);
            q.push(y);
            k += 2;
        }
        out.polys.push((*layer, fill(props), q));
    }
    for (layer, v, props) in &sym.arcs {
        let [cx, cy, rr, a, b] = *v;
        let c = tp(cx, cy, &mut bb);
        let mut a2 = if flip != 0 { 180.0 - a - b } else { a };
        a2 = (a2 - 90.0 * r as f64).rem_euclid(360.0);
        tp(cx + rr, cy + rr, &mut bb);
        tp(cx - rr, cy - rr, &mut bb);
        out.arcs.push((*layer, c.0, c.1, rr, a2, b, fill(props)));
    }
    for t in &sym.texts {
        let text = subst(&t.s, &comp.props, &tmpl, symname);
        let (x, y) = tp(t.x, t.y, &mut bb);
        let trot = (r + if flip != 0 && (t.rot & 1) != 0 { t.rot + 2 } else { t.rot }) & 3;
        emit_text(out, t, text, x, y, trot, flip ^ t.flip)?;
    }
    if bb[0] == f64::INFINITY {
        bb = [ox - 10.0, oy - 10.0, ox + 10.0, oy + 10.0];
    }
    Ok(bb)
}

// ----------------------------------------------------------------------------- render

struct Inst {
    i: usize,
    id: String,
    comp_idx: usize,
    typ: String,
    bbox: [f64; 4],
}

/// render(path, search_paths) -> dict (without "colors", which xschem.py adds).
#[pyfunction]
pub fn render<'py>(py: Python<'py>, path: &str, search_paths: Vec<String>) -> PyResult<Bound<'py, PyDict>> {
    let p = Path::new(path);
    let text = read_lossy(p).ok_or_else(|| pyo3::exceptions::PyFileNotFoundError::new_err(format!("No such file: '{path}'")))?;
    let f = model::parse(&text);
    let base = p.parent();
    let is_symbol = p.extension().is_some_and(|e| e == "sym");
    let mut res = Resolver::new(&search_paths);
    let mut out = Prims::default();

    let ident = XComp {
        sym: String::new(), x: 0.0, y: 0.0, rot: 0, flip: 0,
        props: if is_symbol { model::parse_props(f.global_props.get_or("template", "")) } else { Props::default() },
        embedded: None,
    };
    let fname = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
    emit_symbol(&mut out, &f, &ident, py_stem(fname))?;

    let ids = comp_ids(&f);
    let mut missing: Vec<String> = Vec::new();
    let mut insts: Vec<Inst> = Vec::with_capacity(f.comps.len());
    for (ci, comp) in f.comps.iter().enumerate() {
        let loaded;
        let sym: Option<&XFile> = match &comp.embedded {
            Some(e) => Some(e),
            None => {
                loaded = res.load(&comp.sym, base);
                loaded.as_deref()
            }
        };
        let bb = match sym {
            None => {
                if !missing.contains(&comp.sym) {
                    missing.push(comp.sym.clone());
                }
                let bb = [comp.x - 20.0, comp.y - 20.0, comp.x + 20.0, comp.y + 20.0];
                out.boxes.push((7, bb[0], bb[1], bb[2], bb[3], 0));
                out.texts.push(Text { s: format!("? {}", comp.sym), x: bb[0], y: bb[3] + 2.0, rot: 0, flip: 0,
                                      size: 0.2 * FONT_SCALE, layer: 7, hc: false, vc: false, bold: false, italic: false });
                bb
            }
            Some(s) => emit_symbol(&mut out, s, comp, py_stem(&comp.sym))?,
        };
        insts.push(Inst { i: ci, id: ids[ci].clone(), comp_idx: ci,
                          typ: sym.map(|s| s.global_props.get_or("type", "").to_string()).unwrap_or_default(), bbox: bb });
    }
    missing.sort();

    // bounding box of everything drawn
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    let mut any = false;
    let mut add = |x: f64, y: f64| {
        any = true;
        x0 = x0.min(x);
        y0 = y0.min(y);
        x1 = x1.max(x);
        y1 = y1.max(y);
    };
    for l in &out.lines { add(l.1, l.2); add(l.3, l.4); }
    for b in &out.boxes { add(b.1, b.2); add(b.3, b.4); }
    for w in &f.wires { add(w.0, w.1); add(w.2, w.3); }
    for pl in &out.polys {
        let mut k = 0;
        while k + 1 < pl.2.len() { add(pl.2[k], pl.2[k + 1]); k += 2; }
    }
    for i in &insts { add(i.bbox[0], i.bbox[1]); add(i.bbox[2], i.bbox[3]); }
    for t in &out.texts {
        let tb = text_box(t);
        add(tb[0], tb[1]);
        add(tb[2], tb[3]);
    }
    let bbox = if any { [x0, y0, x1, y1] } else { [-100.0, -100.0, 100.0, 100.0] };

    // to Python
    let d = PyDict::new(py);
    d.set_item("kind", if is_symbol { "symbol" } else { "schematic" })?;
    d.set_item("bbox", PyList::new(py, bbox)?)?;
    let l = PyList::empty(py);
    for x in &out.lines { l.append((x.0, x.1, x.2, x.3, x.4).into_pyobject(py)?.to_list())?; }
    d.set_item("lines", l)?;
    let l = PyList::empty(py);
    for x in &out.boxes { l.append((x.0, x.1, x.2, x.3, x.4, x.5).into_pyobject(py)?.to_list())?; }
    d.set_item("boxes", l)?;
    let l = PyList::empty(py);
    for x in &out.polys { l.append((x.0, x.1, PyList::new(py, &x.2)?).into_pyobject(py)?.to_list())?; }
    d.set_item("polys", l)?;
    let l = PyList::empty(py);
    for x in &out.arcs { l.append((x.0, x.1, x.2, x.3, x.4, x.5, x.6).into_pyobject(py)?.to_list())?; }
    d.set_item("arcs", l)?;
    let l = PyList::empty(py);
    for t in &out.texts {
        let td = PyDict::new(py);
        td.set_item("s", &t.s)?;
        td.set_item("x", t.x)?;
        td.set_item("y", t.y)?;
        td.set_item("rot", t.rot)?;
        td.set_item("flip", t.flip)?;
        td.set_item("size", t.size)?;
        td.set_item("layer", t.layer)?;
        td.set_item("hc", t.hc)?;
        td.set_item("vc", t.vc)?;
        td.set_item("bold", t.bold)?;
        td.set_item("italic", t.italic)?;
        l.append(td)?;
    }
    d.set_item("texts", l)?;
    let l = PyList::empty(py);
    for (wx1, wy1, wx2, wy2, props) in &f.wires {
        l.append((*wx1, *wy1, *wx2, *wy2, props.get_or("bus", "") == "true", props.get_or("lab", ""),
                  wire_id(*wx1, *wy1, *wx2, *wy2)).into_pyobject(py)?.to_list())?;
    }
    d.set_item("wires", l)?;
    let l = PyList::empty(py);
    for it in &insts {
        let c = &f.comps[it.comp_idx];
        let id = PyDict::new(py);
        id.set_item("i", it.i)?;
        id.set_item("id", &it.id)?;
        id.set_item("x", c.x)?;
        id.set_item("y", c.y)?;
        id.set_item("rot", c.rot)?;
        id.set_item("flip", c.flip)?;
        id.set_item("name", c.props.get_or("name", ""))?;
        id.set_item("symbol", &c.sym)?;
        id.set_item("type", &it.typ)?;
        id.set_item("bbox", PyList::new(py, it.bbox)?)?;
        let pd = PyDict::new(py);
        for (k, v) in &c.props.0 {
            if v.chars().count() < 200 {
                pd.set_item(k, v)?;
            }
        }
        id.set_item("props", pd)?;
        l.append(id)?;
    }
    d.set_item("insts", l)?;
    d.set_item("missing", PyList::new(py, &missing)?)?;
    let gp = PyDict::new(py);
    for (k, v) in &f.global_props.0 {
        if matches!(k.as_str(), "type" | "format" | "template") {
            gp.set_item(k, v)?;
        }
    }
    d.set_item("props", gp)?;
    Ok(d)
}
