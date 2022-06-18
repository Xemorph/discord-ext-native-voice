use pyo3::prelude::*;
use pyo3::types::{PyFunction};

use std::sync::Arc;


pub struct Function {
    callback: Arc<Py<PyFunction>>,
}
