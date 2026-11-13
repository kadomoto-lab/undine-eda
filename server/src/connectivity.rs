//! extract(): schematic connectivity (wires, pins, net labels) as undine/schematic.py `_py_extract` computed it
//! (the tests compare the two), returning the same Python objects (Connectivity, Device, XComp, XFile).

use std::collections::HashMap;
use std::path::Path;
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::model::{self, Classes, XComp, XFile};
use crate::render::{comp_ids, py_stem, read_lossy, rot, wire_id, Resolver, PIN_LAYER};

const NAMING_TYPES: [&str; 4] = ["label", "ipin", "opin", "iopin"];
const PORT_TYPES: [&str; 3] = ["ipin", "opin", "iopin"];
const SKIP_TYPES: [&str; 18] = ["label", "ipin", "opin", "iopin", "noconn", "probe", "show_label", "launcher",
                                 "arch_declarations", "architecture", "attributes", "package", "port_attributes",
                                 "netlist_options", "bus_connect", "bus_tap", "generic", "param"];
const CODE_TYPES: [&str; 2] = ["netlist_commands", "netlist_options"];

#[inline]
fn on_segment(px: f64, py: f64, x1: f64, y1: f64, x2: f64, y2: f64) -> bool {
    let eps = 1e-6;
    if x1.min(x2) - eps <= px && px <= x1.max(x2) + eps && y1.min(y2) - eps <= py && py <= y1.max(y2) + eps {
        return ((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)).abs() < eps * 1f64.max((x2 - x1).abs() + (y2 - y1).abs());
    }
    false
}

struct Uf(Vec<usize>);
impl Uf {
    fn find(&mut self, mut a: usize) -> usize {
        while self.0[a] != a {
            self.0[a] = self.0[self.0[a]];
            a = self.0[a];
        }
        a
    }
    fn union(&mut self, a: usize, b: usize) {
        let (ra, rb) = (self.find(a), self.find(b));
        self.0[ra] = rb;
    }
}

/// Python's round(x, 4) as a hashable key (ties to even; coordinates are on a coarse grid in practice).
#[inline]
fn key4(v: f64) -> i64 {
    (v * 1e4).round_ties_even() as i64
}

pub struct Pin {
    pub name: String,
    pub x: f64,
    pub y: f64,
}

pub fn sym_pins(sym: &XFile) -> Vec<Pin> {
    sym.boxes
        .iter()
        .filter(|b| b.0 == PIN_LAYER)
        .map(|(_, x1, y1, x2, y2, pr)| Pin { name: pr.get_or("name", "").to_string(), x: (x1 + x2) / 2.0, y: (y1 + y2) / 2.0 })
        .collect()
}

struct Dev {
    name: String,
    comp: usize,
    sym: Arc<XFile>,
    typ: String,
    pins: Vec<(String, f64, f64)>,
    nets: Vec<(String, String)>, // pin -> net, dict semantics
}

/// An ordered dict of lists keyed by usize / String.
struct OrderedLists<K: std::hash::Hash + Eq + Clone, V> {
    keys: Vec<K>,
    idx: HashMap<K, usize>,
    vals: Vec<Vec<V>>,
}
impl<K: std::hash::Hash + Eq + Clone, V> OrderedLists<K, V> {
    fn new() -> Self {
        OrderedLists { keys: Vec::new(), idx: HashMap::new(), vals: Vec::new() }
    }
    fn entry(&mut self, k: K) -> &mut Vec<V> {
        let i = match self.idx.get(&k) {
            Some(&i) => i,
            None => {
                self.keys.push(k.clone());
                self.vals.push(Vec::new());
                self.idx.insert(k, self.keys.len() - 1);
                self.keys.len() - 1
            }
        };
        &mut self.vals[i]
    }
}

#[pyfunction]
#[pyo3(signature = (path, search_paths, cell, xfile, xcomp, xtext, device, connectivity))]
#[allow(clippy::too_many_arguments)]
pub fn extract<'py>(py: Python<'py>, path: &str, search_paths: Vec<String>, cell: Option<String>,
                    xfile: Bound<'py, PyAny>, xcomp: Bound<'py, PyAny>, xtext: Bound<'py, PyAny>,
                    device: Bound<'py, PyAny>, connectivity: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let p = Path::new(path);
    let text = read_lossy(p).ok_or_else(|| pyo3::exceptions::PyFileNotFoundError::new_err(format!("No such file: '{path}'")))?;
    let f = model::parse(&text);
    let base = p.parent();
    let mut res = Resolver::new(&search_paths);
    let mut warnings: Vec<String> = Vec::new();
    let mut devices: Vec<Dev> = Vec::new();
    let mut labels: Vec<(String, f64, f64)> = Vec::new();
    let mut ports: Vec<(String, String)> = Vec::new();
    let mut code: Vec<(usize, Arc<XFile>)> = Vec::new();
    let nwires = f.wires.len();
    let ids = comp_ids(&f);

    for (i, c) in f.comps.iter().enumerate() {
        let sym: Option<Arc<XFile>> = match &c.embedded {
            Some(e) => Some(Arc::new((**e).clone())),
            None => res.load(&c.sym, base),
        };
        let Some(sym) = sym else {
            warnings.push(format!("missing symbol {} (instance {})", c.sym, c.props.get_or("name", "?")));
            continue;
        };
        let typ = sym.global_props.get_or("type", "").to_string();
        let absp: Vec<(String, f64, f64)> = sym_pins(&sym)
            .into_iter()
            .map(|pn| {
                let (rx, ry) = rot(c.rot, c.flip, pn.x, pn.y);
                (pn.name, rx + c.x, ry + c.y)
            })
            .collect();
        if CODE_TYPES.contains(&typ.as_str()) {
            code.push((i, sym));
            continue;
        }
        if NAMING_TYPES.contains(&typ.as_str()) {
            // inst_props: the symbol's template, overridden by the instance
            let lab = c.props.get("lab").map(str::to_string).unwrap_or_else(|| {
                model::parse_props(sym.global_props.get_or("template", "")).get_or("lab", "").to_string()
            });
            if PORT_TYPES.contains(&typ.as_str()) {
                ports.push((lab.clone(), typ.clone()));
            }
            if !absp.is_empty() && !lab.is_empty() {
                labels.push((lab, absp[0].1, absp[0].2));
            }
            continue;
        }
        if SKIP_TYPES.contains(&typ.as_str()) {
            continue;
        }
        devices.push(Dev { name: ids[i].clone(), comp: i, sym, typ, pins: absp, nets: Vec::new() });
    }

    // union-find nodes: wires first, then device pins, then labels
    let mut all_pts: Vec<(i64, usize, String, f64, f64)> = Vec::new(); // kind (0 dev pin, 1 label), owner, pin, x, y
    for (di, d) in devices.iter().enumerate() {
        for (pn, x, y) in &d.pins {
            all_pts.push((0, di, pn.clone(), *x, *y));
        }
    }
    for (li, (_, x, y)) in labels.iter().enumerate() {
        all_pts.push((1, li, String::new(), *x, *y));
    }
    let mut uf = Uf((0..nwires + all_pts.len()).collect());
    let w = &f.wires;
    let eps = 1e-6;
    // per wire: bbox grown by eps (any touch found by on_segment lies inside both boxes)
    let bb: Vec<[f64; 4]> = w.iter().map(|(x1, y1, x2, y2, _)| {
        [x1.min(*x2) - eps, y1.min(*y2) - eps, x1.max(*x2) + eps, y1.max(*y2) + eps]
    }).collect();
    // wires touching wires: sweep over x, test only pairs whose boxes overlap (the same test as before)
    let mut order: Vec<usize> = (0..nwires).collect();
    order.sort_by(|&a, &b| bb[a][0].total_cmp(&bb[b][0]));
    for (oi, &a) in order.iter().enumerate() {
        for &b in &order[oi + 1..] {
            if bb[b][0] > bb[a][2] {
                break;
            }
            if bb[b][1] > bb[a][3] || bb[a][1] > bb[b][3] {
                continue;
            }
            let (a, b) = (a.min(b), a.max(b));
            let (ax1, ay1, ax2, ay2, _) = &w[a];
            let (bx1, by1, bx2, by2, _) = &w[b];
            if on_segment(*ax1, *ay1, *bx1, *by1, *bx2, *by2) || on_segment(*ax2, *ay2, *bx1, *by1, *bx2, *by2)
                || on_segment(*bx1, *by1, *ax1, *ay1, *ax2, *ay2) || on_segment(*bx2, *by2, *ax1, *ay1, *ax2, *ay2) {
                uf.union(a, b);
            }
        }
    }
    // pins / labels onto wires: wires bucketed on a grid (very long wires are always tested)
    let mut lens: Vec<f64> = bb.iter().map(|b| (b[2] - b[0]).max(b[3] - b[1])).collect();
    lens.sort_by(f64::total_cmp);
    let cell_size = lens.get(lens.len() / 2).copied().unwrap_or(1.0).max(1.0);
    let cell_of = |v: f64| (v / cell_size).floor() as i64;
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut always: Vec<usize> = Vec::new();
    for (wi, b) in bb.iter().enumerate() {
        let (cx0, cy0, cx1, cy1) = (cell_of(b[0]), cell_of(b[1]), cell_of(b[2]), cell_of(b[3]));
        if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > 4096 {
            always.push(wi);
            continue;
        }
        for cx in cx0..=cx1 {
            for cy in cy0..=cy1 {
                grid.entry((cx, cy)).or_default().push(wi);
            }
        }
    }
    let mut by_point: HashMap<(i64, i64), usize> = HashMap::new();
    for (k, (_, _, _, x, y)) in all_pts.iter().enumerate() {
        let node = nwires + k;
        let cands = grid.get(&(cell_of(*x), cell_of(*y))).map(Vec::as_slice).unwrap_or(&[]);
        for &wi in cands.iter().chain(always.iter()) {
            let (x1, y1, x2, y2, _) = &w[wi];
            if on_segment(*x, *y, *x1, *y1, *x2, *y2) {
                uf.union(node, wi);
            }
        }
        match by_point.get(&(key4(*x), key4(*y))) {
            Some(&o) => uf.union(node, o),
            None => {
                by_point.insert((key4(*x), key4(*y)), node);
            }
        }
    }

    // names: wire labels, then label pins; nets sharing a name are one net
    let mut root_names: OrderedLists<usize, String> = OrderedLists::new();
    for (wi, wire) in w.iter().enumerate() {
        let lab = wire.4.get_or("lab", "");
        if !lab.is_empty() {
            let r = uf.find(wi);
            root_names.entry(r).push(lab.to_string());
        }
    }
    for (k, (kind, owner, _, _, _)) in all_pts.iter().enumerate() {
        if *kind == 1 {
            let r = uf.find(nwires + k);
            root_names.entry(r).push(labels[*owner].0.clone());
        }
    }
    let mut name_root: HashMap<String, usize> = HashMap::new();
    for (ri, r) in root_names.keys.clone().iter().enumerate() {
        for n in &root_names.vals[ri] {
            match name_root.get(n) {
                Some(&o) => uf.union(*r, o),
                None => {
                    name_root.insert(n.clone(), *r);
                }
            }
        }
    }
    let mut pool: OrderedLists<usize, String> = OrderedLists::new();
    for (ri, r) in root_names.keys.clone().iter().enumerate() {
        let fr = uf.find(*r);
        let names = root_names.vals[ri].clone();
        pool.entry(fr).extend(names);
    }
    let mut final_names: HashMap<usize, String> = HashMap::new();
    for (pi, root) in pool.keys.iter().enumerate() {
        let names = &pool.vals[pi];
        let chosen = names.iter().find(|n| ports.iter().any(|p| &p.0 == *n)).unwrap_or(&names[0]);
        final_names.insert(*root, chosen.clone());
    }
    let mut counter = 0;
    let mut net_of = |node: usize, uf: &mut Uf| -> String {
        let r = uf.find(node);
        final_names
            .entry(r)
            .or_insert_with(|| {
                counter += 1;
                format!("net{counter}")
            })
            .clone()
    };
    let wire_net: Vec<String> = (0..nwires).map(|wi| net_of(wi, &mut uf)).collect();
    let mut net_points: OrderedLists<String, (f64, f64)> = OrderedLists::new();
    for (wi, nm) in wire_net.iter().enumerate() {
        let e = net_points.entry(nm.clone());
        e.push((w[wi].0, w[wi].1));
        e.push((w[wi].2, w[wi].3));
    }
    let mut nets: OrderedLists<String, (String, String)> = OrderedLists::new();
    for (k, (kind, owner, pn, x, y)) in all_pts.iter().enumerate() {
        let nm = net_of(nwires + k, &mut uf);
        if *kind == 0 {
            let d = &mut devices[*owner];
            match d.nets.iter_mut().find(|(a, _)| a == pn) {
                Some(e) => e.1 = nm.clone(),
                None => d.nets.push((pn.clone(), nm.clone())),
            }
            nets.entry(nm.clone()).push((d.name.clone(), pn.clone()));
        }
        net_points.entry(nm).push((*x, *y));
    }
    for nm in wire_net.iter().chain(net_points.keys.clone().iter()) {
        nets.entry(nm.clone());
    }

    // ----- to Python
    let cls = Classes { xfile, xcomp, xtext };
    let mut sym_py: HashMap<*const XFile, Bound<'py, PyAny>> = HashMap::new();
    let mut sym_obj = |s: &Arc<XFile>| -> PyResult<Bound<'py, PyAny>> {
        let k = Arc::as_ptr(s);
        if let Some(o) = sym_py.get(&k) {
            return Ok(o.clone());
        }
        let o = model::to_py(py, &cls, s)?;
        sym_py.insert(k, o.clone());
        Ok(o)
    };
    let comp_obj = |c: &XComp| -> PyResult<Bound<'py, PyAny>> {
        let pc = cls.xcomp.call1((c.sym.as_str(), c.x, c.y, c.rot, c.flip, c.props.to_py(py)?))?;
        if let Some(e) = &c.embedded {
            pc.setattr("embedded", model::to_py(py, &cls, e)?)?;
        }
        Ok(pc)
    };
    let pdevs = PyList::empty(py);
    for d in &devices {
        let c = &f.comps[d.comp];
        let pins = PyList::empty(py);
        for (pn, x, y) in &d.pins {
            pins.append((pn.as_str(), *x, *y))?;
        }
        let pnets = PyDict::new(py);
        for (a, b) in &d.nets {
            pnets.set_item(a, b)?;
        }
        let pd = device.call1((d.name.as_str(), comp_obj(c)?, d.comp, sym_obj(&d.sym)?, py_stem(&c.sym), d.typ.as_str(), pins))?;
        pd.setattr("nets", pnets)?;
        pdevs.append(pd)?;
    }
    let pnets = PyDict::new(py);
    for (i, k) in nets.keys.iter().enumerate() {
        let l = PyList::empty(py);
        for (a, b) in &nets.vals[i] {
            l.append((a.as_str(), b.as_str()))?;
        }
        pnets.set_item(k, l)?;
    }
    let pports = PyList::empty(py);
    for (a, b) in &ports {
        pports.append((a.as_str(), b.as_str()))?;
    }
    let pnp = PyDict::new(py);
    for (i, k) in net_points.keys.iter().enumerate() {
        let l = PyList::empty(py);
        for (x, y) in &net_points.vals[i] {
            l.append((*x, *y))?;
        }
        pnp.set_item(k, l)?;
    }
    let pcode = PyList::empty(py);
    for (ci, s) in &code {
        pcode.append(PyTuple::new(py, [comp_obj(&f.comps[*ci])?, sym_obj(s)?])?)?;
    }
    let wire_ids: Vec<String> = w.iter().map(|x| wire_id(x.0, x.1, x.2, x.3)).collect();
    let cell = cell.unwrap_or_else(|| py_stem(p.file_name().and_then(|s| s.to_str()).unwrap_or("")).to_string());
    connectivity.call1((cell, pdevs, pnets, pports, PyList::new(py, &wire_net)?, PyList::new(py, &wire_ids)?, pnp, pcode,
                        PyList::new(py, &warnings)?))
}
