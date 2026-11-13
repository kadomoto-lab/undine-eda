//! undine_core: the parts of the Undine server where Python itself was the bottleneck, in Rust.

use pyo3::prelude::*;
use pyo3::types::PyDict;

mod connectivity;
mod model;
mod render;
mod writer;

/// wire_id(x1, y1, x2, y2) -> "x1,y1,x2,y2" (end points sorted, %g)
#[pyfunction]
fn wire_id(x1: f64, y1: f64, x2: f64, y2: f64) -> String {
    render::wire_id(x1, y1, x2, y2)
}

/// parse(text, XFile, XComp, XText) -> XFile (the classes of undine/xschem.py)
#[pyfunction]
fn parse<'py>(py: Python<'py>, text: &str, xfile: Bound<'py, PyAny>, xcomp: Bound<'py, PyAny>,
              xtext: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    model::to_py(py, &model::Classes { xfile, xcomp, xtext }, &model::parse(text))
}

/// parse_props(s) -> dict
#[pyfunction]
fn parse_props<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyDict>> {
    model::parse_props(s).to_py(py)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse, m)?)?;
    m.add_function(wrap_pyfunction!(parse_props, m)?)?;
    m.add_function(wrap_pyfunction!(render::render, m)?)?;
    m.add_function(wrap_pyfunction!(connectivity::extract, m)?)?;
    m.add_function(wrap_pyfunction!(writer::dump, m)?)?;
    m.add_function(wrap_pyfunction!(wire_id, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
