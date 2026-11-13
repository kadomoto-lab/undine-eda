//! The Xschem file model in Rust, and its reader. `to_py` builds the Python objects of undine/xschem.py
//! (XFile / XComp / XText) from it; render / connectivity work on it directly.
//!
//! The reader gives the same results as the former pure-Python reader (`_py_parse` / `_py_parse_props`,
//! kept in xschem.py and compared in the tests). All delimiters of the format are ASCII, so it scans
//! bytes; a backslash escape takes the next whole character (UTF-8), like Python's string indexing did.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

/// Attributes in order of first appearance; a repeated key keeps its first position (Python dict semantics).
#[derive(Clone, Debug, Default)]
pub struct Props(pub Vec<(String, String)>);

impl Props {
    pub fn get(&self, k: &str) -> Option<&str> {
        self.0.iter().find(|(a, _)| a == k).map(|(_, v)| v.as_str())
    }
    pub fn get_or<'a>(&'a self, k: &str, d: &'a str) -> &'a str {
        self.get(k).unwrap_or(d)
    }
    pub fn contains(&self, k: &str) -> bool {
        self.0.iter().any(|(a, _)| a == k)
    }
    pub fn set(&mut self, k: &str, v: String) {
        match self.0.iter_mut().find(|(a, _)| a == k) {
            Some(e) => e.1 = v,
            None => self.0.push((k.to_string(), v)),
        }
    }
    pub fn update(&mut self, o: &Props) {
        for (k, v) in &o.0 {
            self.set(k, v.clone());
        }
    }
    pub fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        for (k, v) in &self.0 {
            d.set_item(k, v)?;
        }
        Ok(d)
    }
}

#[derive(Clone, Debug)]
pub struct XText {
    pub s: String,
    pub x: f64,
    pub y: f64,
    pub rot: i64,
    pub flip: i64,
    pub xs: f64,
    pub ys: f64,
    pub props: Props,
}

#[derive(Clone, Debug)]
pub struct XComp {
    pub sym: String,
    pub x: f64,
    pub y: f64,
    pub rot: i64,
    pub flip: i64,
    pub props: Props,
    pub embedded: Option<Box<XFile>>,
}

#[derive(Clone, Debug, Default)]
pub struct XFile {
    pub lines: Vec<(i64, f64, f64, f64, f64, Props)>,
    pub boxes: Vec<(i64, f64, f64, f64, f64, Props)>,
    pub polys: Vec<(i64, Vec<f64>, Props)>,
    pub arcs: Vec<(i64, [f64; 5], Props)>,
    pub texts: Vec<XText>,
    pub wires: Vec<(f64, f64, f64, f64, Props)>,
    pub comps: Vec<XComp>,
    pub global_props: Props,
    pub g_text: String,
    pub code: Vec<(String, String)>,
    pub version: String,
    pub k_raw: String,
}

#[inline]
pub fn is_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\r' | b'\n')
}

#[inline]
fn char_len(s: &str, i: usize) -> usize {
    s[i..].chars().next().map_or(1, char::len_utf8)
}

/// Python's float(): what Rust accepts plus digit-separating underscores ("1_000").
pub fn py_float(w: &str) -> Option<f64> {
    if let Ok(v) = w.parse::<f64>() {
        return Some(v);
    }
    if w.contains('_') {
        let b = w.as_bytes();
        let ok = b.iter().enumerate().all(|(k, &c)| {
            c != b'_' || (k > 0 && k + 1 < b.len() && b[k - 1].is_ascii_digit() && b[k + 1].is_ascii_digit())
        });
        if ok {
            return w.replace('_', "").parse::<f64>().ok();
        }
    }
    None
}

/// int(float) as Python truncates; NaN / infinities (which raised in Python) become 0 / saturate.
#[inline]
fn trunc_int(v: f64) -> i64 {
    v as i64
}

struct Reader<'a> {
    s: &'a str,
    b: &'a [u8],
    i: usize,
}

impl<'a> Reader<'a> {
    fn new(s: &'a str) -> Self {
        Reader { s, b: s.as_bytes(), i: 0 }
    }
    fn skip_ws(&mut self) {
        while self.i < self.b.len() && is_ws(self.b[self.i]) {
            self.i += 1;
        }
    }
    fn eof(&mut self) -> bool {
        self.skip_ws();
        self.i >= self.b.len()
    }
    fn peek(&mut self) -> Option<u8> {
        self.skip_ws();
        self.b.get(self.i).copied()
    }
    fn next_char(&mut self) -> &'a str {
        self.skip_ws();
        let j = self.i;
        self.i += char_len(self.s, j);
        &self.s[j..self.i]
    }
    fn word(&mut self) -> &'a str {
        self.skip_ws();
        let j = self.i;
        while self.i < self.b.len() && !matches!(self.b[self.i], b' ' | b'\t' | b'\r' | b'\n' | b'{' | b'}' | b'[' | b']') {
            self.i += 1;
        }
        &self.s[j..self.i]
    }
    fn num(&mut self) -> f64 {
        py_float(self.word()).unwrap_or(0.0)
    }
    fn brace(&mut self) -> String {
        self.skip_ws();
        if self.i >= self.b.len() || self.b[self.i] != b'{' {
            return self.word().to_string();
        }
        self.i += 1;
        let mut depth = 1;
        let mut out: Vec<u8> = Vec::new();
        let n = self.b.len();
        while self.i < n {
            let c = self.b[self.i];
            if c == b'\\' && self.i + 1 < n {
                let l = char_len(self.s, self.i + 1);
                out.extend_from_slice(&self.b[self.i + 1..self.i + 1 + l]);
                self.i += 1 + l;
                continue;
            }
            if c == b'{' {
                depth += 1;
            } else if c == b'}' {
                depth -= 1;
                if depth == 0 {
                    self.i += 1;
                    break;
                }
            }
            out.push(c);
            self.i += 1;
        }
        String::from_utf8(out).unwrap_or_default() // whole characters only: valid UTF-8
    }
    fn bracket_block(&mut self) -> &'a str {
        self.skip_ws();
        self.i += 1; // '['
        let n = self.b.len();
        let j = self.i.min(n);
        let mut depth: i64 = 0;
        while self.i < n {
            let c = self.b[self.i];
            if c == b'\\' {
                self.i += 1;
                if self.i < n {
                    self.i += char_len(self.s, self.i);
                }
                continue;
            }
            if c == b'{' {
                depth += 1;
            } else if c == b'}' {
                depth -= 1;
            } else if c == b']' && depth == 0 {
                let block = &self.s[j..self.i];
                self.i += 1;
                return block;
            }
            self.i += 1;
        }
        &self.s[j..]
    }
    fn skip_line(&mut self) {
        while self.i < self.b.len() && self.b[self.i] != b'\n' {
            self.i += 1;
        }
    }
}

/// key=value key2="quoted value" ...
pub fn parse_props(s: &str) -> Props {
    let mut p = Props::default();
    let b = s.as_bytes();
    let n = b.len();
    let mut i = 0;
    while i < n {
        while i < n && is_ws(b[i]) {
            i += 1;
        }
        let j = i;
        while i < n && b[i] != b'=' && !is_ws(b[i]) {
            i += 1;
        }
        let key = &s[j..i];
        if i >= n || b[i] != b'=' {
            if !key.is_empty() && !p.contains(key) {
                p.0.push((key.to_string(), String::new()));
            }
            continue;
        }
        i += 1;
        if i < n && b[i] == b'"' {
            i += 1;
            let mut v: Vec<u8> = Vec::new();
            while i < n && b[i] != b'"' {
                if b[i] == b'\\' && i + 1 < n {
                    let l = char_len(s, i + 1);
                    v.extend_from_slice(&b[i + 1..i + 1 + l]);
                    i += 1 + l;
                    continue;
                }
                v.push(b[i]);
                i += 1;
            }
            i += 1;
            p.set(key, String::from_utf8(v).unwrap_or_default());
        } else {
            let j = i;
            while i < n && !is_ws(b[i]) {
                i += 1;
            }
            p.set(key, s[j..i].to_string());
        }
    }
    p
}

pub fn parse(text: &str) -> XFile {
    let mut r = Reader::new(text);
    let mut f = XFile::default();
    while !r.eof() {
        let t = r.next_char();
        match t {
            "v" => f.version = r.brace(),
            "G" | "K" => {
                let raw = r.brace();
                f.global_props.update(&parse_props(&raw));
                if t == "K" {
                    f.k_raw = raw;
                } else {
                    f.g_text = raw;
                }
            }
            "V" | "S" | "E" | "F" => {
                let v = r.brace();
                match f.code.iter_mut().find(|(k, _)| k == t) {
                    Some(e) => e.1 = v,
                    None => f.code.push((t.to_string(), v)),
                }
            }
            "L" | "B" => {
                let layer = trunc_int(r.num());
                let (x1, y1, x2, y2) = (r.num(), r.num(), r.num(), r.num());
                let props = parse_props(&r.brace());
                let e = (layer, x1, y1, x2, y2, props);
                if t == "L" { f.lines.push(e) } else { f.boxes.push(e) }
            }
            "P" => {
                let layer = trunc_int(r.num());
                let npts = trunc_int(r.num()).max(0) as usize;
                let pts: Vec<f64> = (0..2 * npts).map(|_| r.num()).collect();
                f.polys.push((layer, pts, parse_props(&r.brace())));
            }
            "A" => {
                let layer = trunc_int(r.num());
                let v = [r.num(), r.num(), r.num(), r.num(), r.num()];
                f.arcs.push((layer, v, parse_props(&r.brace())));
            }
            "T" => {
                let s = r.brace();
                let (x, y, rot, flip, xs, ys) = (r.num(), r.num(), r.num(), r.num(), r.num(), r.num());
                f.texts.push(XText { s, x, y, rot: trunc_int(rot) & 3, flip: trunc_int(flip) & 1, xs, ys,
                                     props: parse_props(&r.brace()) });
            }
            "N" => {
                let (x1, y1, x2, y2) = (r.num(), r.num(), r.num(), r.num());
                f.wires.push((x1, y1, x2, y2, parse_props(&r.brace())));
            }
            "C" => {
                let sym = r.brace();
                let (x, y, rot, flip) = (r.num(), r.num(), r.num(), r.num());
                let props = parse_props(&r.brace());
                let embedded = if r.peek() == Some(b'[') { Some(Box::new(parse(r.bracket_block()))) } else { None };
                f.comps.push(XComp { sym, x, y, rot: trunc_int(rot) & 3, flip: trunc_int(flip) & 1, props, embedded });
            }
            _ => r.skip_line(), // comments (# *) and unknown records
        }
    }
    f
}

// ----------------------------------------------------------------------------- to Python

pub struct Classes<'py> {
    pub xfile: Bound<'py, PyAny>,
    pub xcomp: Bound<'py, PyAny>,
    pub xtext: Bound<'py, PyAny>,
}

fn tup<'py>(py: Python<'py>, items: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyTuple>> {
    let _ = py;
    PyTuple::new(py, items)
}

pub fn to_py<'py>(py: Python<'py>, cls: &Classes<'py>, f: &XFile) -> PyResult<Bound<'py, PyAny>> {
    let o = cls.xfile.call0()?;
    let fl = |v: f64| -> PyResult<Bound<'py, PyAny>> { Ok(v.into_pyobject(py)?.into_any()) };
    let it = |v: i64| -> PyResult<Bound<'py, PyAny>> { Ok(v.into_pyobject(py)?.into_any()) };
    let rect = |v: &Vec<(i64, f64, f64, f64, f64, Props)>| -> PyResult<Bound<'py, PyList>> {
        let l = PyList::empty(py);
        for (a, b, c, d, e, p) in v {
            l.append(tup(py, vec![it(*a)?, fl(*b)?, fl(*c)?, fl(*d)?, fl(*e)?, p.to_py(py)?.into_any()])?)?;
        }
        Ok(l)
    };
    o.setattr("lines", rect(&f.lines)?)?;
    o.setattr("boxes", rect(&f.boxes)?)?;
    let polys = PyList::empty(py);
    for (layer, pts, p) in &f.polys {
        polys.append(tup(py, vec![it(*layer)?, PyList::new(py, pts)?.into_any(), p.to_py(py)?.into_any()])?)?;
    }
    o.setattr("polys", polys)?;
    let arcs = PyList::empty(py);
    for (layer, v, p) in &f.arcs {
        arcs.append(tup(py, vec![it(*layer)?, fl(v[0])?, fl(v[1])?, fl(v[2])?, fl(v[3])?, fl(v[4])?, p.to_py(py)?.into_any()])?)?;
    }
    o.setattr("arcs", arcs)?;
    let texts = PyList::empty(py);
    for t in &f.texts {
        texts.append(cls.xtext.call1((t.s.as_str(), t.x, t.y, t.rot, t.flip, t.xs, t.ys, t.props.to_py(py)?))?)?;
    }
    o.setattr("texts", texts)?;
    let wires = PyList::empty(py);
    for (a, b, c, d, p) in &f.wires {
        wires.append(tup(py, vec![fl(*a)?, fl(*b)?, fl(*c)?, fl(*d)?, p.to_py(py)?.into_any()])?)?;
    }
    o.setattr("wires", wires)?;
    let comps = PyList::empty(py);
    for c in &f.comps {
        let pc = cls.xcomp.call1((c.sym.as_str(), c.x, c.y, c.rot, c.flip, c.props.to_py(py)?))?;
        if let Some(e) = &c.embedded {
            pc.setattr("embedded", to_py(py, cls, e)?)?;
        }
        comps.append(pc)?;
    }
    o.setattr("comps", comps)?;
    o.setattr("global_props", f.global_props.to_py(py)?)?;
    o.setattr("g_text", f.g_text.as_str())?;
    let code = PyDict::new(py);
    for (k, v) in &f.code {
        code.set_item(k, v)?;
    }
    o.setattr("code", code)?;
    o.setattr("version", f.version.as_str())?;
    o.setattr("k_raw", f.k_raw.as_str())?;
    Ok(o)
}
