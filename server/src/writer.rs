//! dump(): an XFile (the Python object of undine/xschem.py) in Xschem's text format, exactly as the former
//! Python writer (`_py_dump`, compared in the tests) wrote it.

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::render::py_gp;

/// backslash and braces escaped, as inside { } groups
fn esc(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '\\' => o.push_str("\\\\"),
            '{' => o.push_str("\\{"),
            '}' => o.push_str("\\}"),
            _ => o.push(c),
        }
    }
    o
}

/// format(v, ".10g") of a Python number
fn n(v: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(f) = v.downcast::<PyFloat>() {
        return Ok(py_gp(f.value(), 10));
    }
    Ok(py_gp(v.extract::<f64>()?, 10))
}

/// str() of a value; strings and ints without a Python call
fn s_of(v: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = v.downcast::<PyString>() {
        return Ok(s.to_str()?.to_string());
    }
    if v.is_instance_of::<PyInt>() {
        if let Ok(i) = v.extract::<i64>() {
            return Ok(i.to_string());
        }
    }
    Ok(v.str()?.to_string())
}

fn props(d: &Bound<'_, PyAny>) -> PyResult<String> {
    let d = d.downcast::<PyDict>()?;
    let mut parts: Vec<String> = Vec::with_capacity(d.len());
    let mut multiline = false;
    for (k, v) in d.iter() {
        let k = s_of(&k)?;
        let v = s_of(&v)?;
        multiline |= v.contains('\n');
        if v.is_empty() || v.chars().any(|c| matches!(c, ' ' | '\t' | '\r' | '\n' | '"' | '=' | '\\')) {
            parts.push(format!("{k}=\"{}\"", v.replace('\\', "\\\\").replace('"', "\\\"")));
        } else {
            parts.push(format!("{k}={v}"));
        }
    }
    Ok(parts.join(if multiline { "\n" } else { " " }))
}

fn dump_into(f: &Bound<'_, PyAny>, embedded: bool, o: &mut Vec<String>) -> PyResult<()> {
    if !embedded {
        let v: String = f.getattr(intern!(f.py(), "version"))?.extract()?;
        o.push(format!("v {{{}}}", esc(if v.is_empty() { "xschem version=3.4.5 file_version=1.2\n" } else { &v })));
    }
    let g_text: String = f.getattr(intern!(f.py(), "g_text"))?.extract()?;
    o.push(format!("G {{{}}}", esc(&g_text)));
    let k_raw: String = f.getattr(intern!(f.py(), "k_raw"))?.extract()?;
    let gp = f.getattr(intern!(f.py(), "global_props"))?;
    if !k_raw.is_empty() || (gp.is_truthy()? && g_text.is_empty()) {
        let k = if k_raw.is_empty() { props(&gp)? } else { k_raw };
        o.push(format!("K {{{}}}", esc(&k)));
    }
    let code = f.getattr(intern!(f.py(), "code"))?;
    let code = code.downcast::<PyDict>()?;
    for k in ["V", "S", "E", "F"] {
        if let Some(v) = code.get_item(k)? {
            o.push(format!("{k} {{{}}}", esc(&v.str()?.to_string())));
        }
    }
    for (rec, attr) in [("L", "lines"), ("B", "boxes")] {
        for e in f.getattr(attr)?.downcast::<PyList>()?.iter() {
            let t = e.downcast::<PyTuple>()?;
            o.push(format!("{rec} {} {} {} {} {} {{{}}}", s_of(&t.get_item(0)?)?, n(&t.get_item(1)?)?, n(&t.get_item(2)?)?,
                           n(&t.get_item(3)?)?, n(&t.get_item(4)?)?, esc(&props(&t.get_item(5)?)?)));
        }
    }
    for e in f.getattr(intern!(f.py(), "polys"))?.downcast::<PyList>()?.iter() {
        let t = e.downcast::<PyTuple>()?;
        let pts = t.get_item(1)?;
        let vals: Vec<String> = pts.try_iter()?.map(|p| n(&p?)).collect::<PyResult<_>>()?;
        o.push(format!("P {} {} {} {{{}}}", s_of(&t.get_item(0)?)?, vals.len() / 2, vals.join(" "), esc(&props(&t.get_item(2)?)?)));
    }
    for e in f.getattr(intern!(f.py(), "arcs"))?.downcast::<PyList>()?.iter() {
        let t = e.downcast::<PyTuple>()?;
        o.push(format!("A {} {} {} {} {} {} {{{}}}", s_of(&t.get_item(0)?)?, n(&t.get_item(1)?)?, n(&t.get_item(2)?)?,
                       n(&t.get_item(3)?)?, n(&t.get_item(4)?)?, n(&t.get_item(5)?)?, esc(&props(&t.get_item(6)?)?)));
    }
    for t in f.getattr(intern!(f.py(), "texts"))?.downcast::<PyList>()?.iter() {
        let s: String = t.getattr(intern!(f.py(), "s"))?.extract()?;
        o.push(format!("T {{{}}} {} {} {} {} {} {} {{{}}}", esc(&s), n(&t.getattr(intern!(f.py(), "x"))?)?, n(&t.getattr(intern!(f.py(), "y"))?)?,
                       s_of(&t.getattr(intern!(f.py(), "rot"))?)?, s_of(&t.getattr(intern!(f.py(), "flip"))?)?, n(&t.getattr(intern!(f.py(), "xs"))?)?, n(&t.getattr(intern!(f.py(), "ys"))?)?,
                       esc(&props(&t.getattr(intern!(f.py(), "props"))?)?)));
    }
    for e in f.getattr(intern!(f.py(), "wires"))?.downcast::<PyList>()?.iter() {
        let t = e.downcast::<PyTuple>()?;
        o.push(format!("N {} {} {} {} {{{}}}", n(&t.get_item(0)?)?, n(&t.get_item(1)?)?, n(&t.get_item(2)?)?,
                       n(&t.get_item(3)?)?, esc(&props(&t.get_item(4)?)?)));
    }
    for c in f.getattr(intern!(f.py(), "comps"))?.downcast::<PyList>()?.iter() {
        let sym: String = c.getattr(intern!(f.py(), "sym"))?.extract()?;
        let mut line = format!("C {{{}}} {} {} {} {} {{{}}}", esc(&sym), n(&c.getattr(intern!(f.py(), "x"))?)?, n(&c.getattr(intern!(f.py(), "y"))?)?,
                               s_of(&c.getattr(intern!(f.py(), "rot"))?)?, s_of(&c.getattr(intern!(f.py(), "flip"))?)?, esc(&props(&c.getattr(intern!(f.py(), "props"))?)?));
        let emb = c.getattr(intern!(f.py(), "embedded"))?;
        if !emb.is_none() {
            let mut inner = Vec::new();
            dump_into(&emb, true, &mut inner)?;
            line.push_str(" [\n");
            line.push_str(&(inner.join("\n") + "\n"));
            line.push_str("\n]");
        }
        o.push(line);
    }
    Ok(())
}

/// dump(xfile, embedded=False) -> str
#[pyfunction]
#[pyo3(signature = (f, embedded=false))]
pub fn dump(f: &Bound<'_, PyAny>, embedded: bool) -> PyResult<String> {
    let mut o = Vec::new();
    dump_into(f, embedded, &mut o)?;
    Ok(o.join("\n") + "\n")
}
