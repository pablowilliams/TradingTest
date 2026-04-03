use pyo3::prelude::*;
use std::collections::HashMap;

// NOTE (#100): This crate requires PyO3 0.20.x. If upgrading to PyO3 0.21+,
// the module init signature changes from `fn trading_engine(_py: Python, m: &PyModule)`
// to `fn trading_engine(m: &Bound<'_, PyModule>)`. See PyO3 migration guide.

/// Fast orderbook processing
#[pyclass]
struct OrderbookProcessor {
    bids: Vec<(f64, f64)>,
    asks: Vec<(f64, f64)>,
}

#[pymethods]
impl OrderbookProcessor {
    #[new]
    fn new() -> Self {
        OrderbookProcessor {
            bids: Vec::new(),
            asks: Vec::new(),
        }
    }

    fn update(&mut self, bids: Vec<(f64, f64)>, asks: Vec<(f64, f64)>) {
        self.bids = bids;
        self.asks = asks;
        self.bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        self.asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    }

    fn best_bid(&self) -> f64 {
        self.bids.first().map(|b| b.0).unwrap_or(0.0)
    }

    fn best_ask(&self) -> f64 {
        self.asks.first().map(|a| a.0).unwrap_or(1.0)
    }

    fn spread(&self) -> f64 {
        self.best_ask() - self.best_bid()
    }

    fn midpoint(&self) -> f64 {
        (self.best_bid() + self.best_ask()) / 2.0
    }

    fn imbalance(&self) -> f64 {
        let bid_vol: f64 = self.bids.iter().map(|b| b.1).sum();
        let ask_vol: f64 = self.asks.iter().map(|a| a.1).sum();
        let total = bid_vol + ask_vol;
        if total == 0.0 { 0.0 } else { (bid_vol - ask_vol) / total }
    }

    fn bid_depth(&self, levels: usize) -> f64 {
        self.bids.iter().take(levels).map(|b| b.1).sum()
    }

    fn ask_depth(&self, levels: usize) -> f64 {
        self.asks.iter().take(levels).map(|a| a.1).sum()
    }
}

/// Fast signal combination for strategy decisions
/// #74: base_sum is now a parameter instead of hardcoded 0.90
#[pyfunction]
fn combine_signals(
    market_price: f64,
    btc_momentum: f64,
    pm_momentum: f64,
    strategy_signal: f64,
    learning_bias: f64,
    learning_weight: f64,
    late_window_boost: f64,
    time_remaining: f64,
    base_sum: f64,
) -> f64 {
    let scale = if base_sum > 0.0 {
        (1.0 - learning_weight) / base_sum
    } else {
        1.0
    };
    let mut combined = market_price * 0.50 * scale
        + btc_momentum * 0.15 * scale
        + pm_momentum * 0.10 * scale
        + strategy_signal * 0.15 * scale
        + learning_bias * learning_weight;

    if time_remaining < 60.0 {
        combined *= 1.0 + late_window_boost;
    }
    combined
}

/// Soccer 3-way opportunity check
/// #75: Accept draw_index as a parameter instead of guessing.
///      If draw_index is out of range or negative, falls back to
///      finding the middle-priced outcome.
#[pyfunction]
fn check_soccer_3way(
    prices: Vec<f64>,
    buy_threshold: f64,
    sell_threshold: f64,
    risk_threshold: f64,
    entry_combined: f64,
    draw_index: i32,
) -> HashMap<String, f64> {
    let mut result = HashMap::new();

    if prices.len() != 3 {
        result.insert("action".to_string(), 0.0); // hold
        return result;
    }

    // Determine the draw index
    let actual_draw_idx: usize = if draw_index >= 0 && (draw_index as usize) < prices.len() {
        draw_index as usize
    } else {
        // Fallback: assume draw is the middle-priced outcome
        let mut indexed: Vec<(usize, f64)> = prices.iter().enumerate().map(|(i, &p)| (i, p)).collect();
        indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());
        indexed[1].0
    };

    // Sum the two non-draw outcomes (favorite + underdog)
    let mut non_draw_sum = 0.0;
    for (i, &p) in prices.iter().enumerate() {
        if i != actual_draw_idx {
            non_draw_sum += p;
        }
    }
    let combined = non_draw_sum;

    if entry_combined == 0.0 && combined < buy_threshold {
        result.insert("action".to_string(), 1.0); // buy
        result.insert("combined".to_string(), combined);
        result.insert("discount".to_string(), buy_threshold - combined);
    } else if entry_combined > 0.0 && combined >= sell_threshold {
        result.insert("action".to_string(), 2.0); // sell
        result.insert("combined".to_string(), combined);
    } else if entry_combined > 0.0 {
        let drop = (entry_combined - combined) / entry_combined;
        if drop >= risk_threshold {
            result.insert("action".to_string(), 3.0); // flip
            result.insert("drop_pct".to_string(), drop);
        } else {
            result.insert("action".to_string(), 0.0); // hold
        }
    } else {
        result.insert("action".to_string(), 0.0);
    }
    result
}

/// Python module
#[pymodule]
fn trading_engine(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<OrderbookProcessor>()?;
    m.add_function(wrap_pyfunction!(combine_signals, m)?)?;
    m.add_function(wrap_pyfunction!(check_soccer_3way, m)?)?;
    Ok(())
}
