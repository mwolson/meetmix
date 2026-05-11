use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use anyhow::Result;
use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};
use signal_hook::iterator::Signals;

#[derive(Clone)]
pub struct Handles {
    stop: Arc<AtomicBool>,
}

impl Handles {
    pub fn stop_requested(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }
}

pub fn install() -> Result<Handles> {
    let stop = Arc::new(AtomicBool::new(false));
    let stop_for_thread = Arc::clone(&stop);
    let mut signals = Signals::new([SIGHUP, SIGINT, SIGTERM])?;
    std::thread::Builder::new()
        .name("meetmix-signals".into())
        .spawn(move || {
            for _ in &mut signals {
                stop_for_thread.store(true, Ordering::SeqCst);
            }
        })?;
    Ok(Handles { stop })
}
