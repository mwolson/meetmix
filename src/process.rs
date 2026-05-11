use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub fn stop_child(child: &mut Option<Child>) {
    let Some(proc) = child.as_mut() else {
        return;
    };
    if matches!(proc.try_wait(), Ok(Some(_))) {
        return;
    }

    let _ = Command::new("kill")
        .arg("-TERM")
        .arg(proc.id().to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();

    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if matches!(proc.try_wait(), Ok(Some(_))) {
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }

    let _ = proc.kill();
    let _ = proc.wait();
}
