use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use chrono::Local;

use crate::audio::{self, PactlRunner, RealPactl};
use crate::config::Config;
use crate::logging::{self, Logger};
use crate::process;
use crate::signals;
use crate::wav;

const MONITOR_INTERVAL: u32 = 8;
const SCO_WARMUP_SECONDS: u64 = 3;

pub struct Session {
    pub bt_sink_name: Option<String>,
    pub capture_loopback: Option<Child>,
    pub capture_target: String,
    pub device_match: Option<String>,
    pub forwarding_loopback: Option<Child>,
    pub keep_recording: bool,
    pub log_path: Option<PathBuf>,
    pub modules: Vec<(String, String)>,
    pub original_bt_sink_volume: Option<u32>,
    pub original_card_profile: Option<String>,
    pub original_default_sink: Option<String>,
    pub record_proc: Option<Child>,
    pub sco_warmup_proc: Option<Child>,
    pub wav_path: Option<PathBuf>,
}

impl Default for Session {
    fn default() -> Self {
        Self {
            bt_sink_name: None,
            capture_loopback: None,
            capture_target: audio::CAPTURE_SINK_NAME.to_string(),
            device_match: None,
            forwarding_loopback: None,
            keep_recording: false,
            log_path: None,
            modules: Vec::new(),
            original_bt_sink_volume: None,
            original_card_profile: None,
            original_default_sink: None,
            record_proc: None,
            sco_warmup_proc: None,
            wav_path: None,
        }
    }
}

pub fn run_record(config: Config, keep_recording: bool, extra_args: Vec<String>) -> Result<i32> {
    let Some(device_match) = config.device_match.clone() else {
        eprintln!("Error: No --device-match specified.");
        eprintln!("Run 'meetmix devices' to list available audio devices,");
        eprintln!("then set --device-match in ~/.config/meetmix.conf or on the command line.");
        bail!("missing --device-match");
    };

    let timestamp = Local::now().format("%Y%m%d-%H%M%S").to_string();
    let log_path = logging::log_dir()
        .context("HOME is not set")?
        .join(format!("meetmix-{}.log", timestamp));
    let mut logger = Logger::new(&log_path)?;

    let recording_dir = logging::recording_dir().context("HOME is not set")?;
    std::fs::create_dir_all(&recording_dir)?;

    let mut session = Session {
        keep_recording,
        log_path: Some(log_path),
        wav_path: Some(recording_dir.join(format!("meetmix-{}.wav", timestamp))),
        ..Session::default()
    };

    let signals = signals::install()?;
    let runner = RealPactl;
    let mut recording_ran = false;

    let setup_result = setup_pipeline(&runner, &mut session, &device_match, &signals, &mut logger);
    let record_result = match setup_result {
        Ok(()) if !signals.stop_requested() => {
            recording_ran = true;
            run_recording(&runner, &mut session, &signals, &mut logger)
        }
        Ok(()) => {
            logger.log("Stop requested during setup, skipping recording.")?;
            Ok(())
        }
        Err(err) => Err(err),
    };

    cleanup_session(&runner, &mut session, &mut logger)?;
    record_result?;

    if recording_ran {
        process_recording(&mut session, &config, extra_args, &mut logger)
    } else {
        Ok(0)
    }
}

pub fn run_devices(config: &Config) -> Result<()> {
    let runner = RealPactl;
    println!("Sources (microphones):");
    for source in audio::list_sources(&runner)? {
        if source.monitor_of_sink.is_some() {
            continue;
        }
        let marker = match &config.device_match {
            Some(pattern) if audio::matches_device(&source, pattern) => " *",
            _ => "",
        };
        println!("  {}  ({}){}", source.name, source.description, marker);
    }

    println!();
    println!("Sinks (speakers):");
    for sink in audio::list_sinks(&runner)? {
        let marker = match &config.device_match {
            Some(pattern) if audio::matches_device(&sink, pattern) => " *",
            _ => "",
        };
        println!("  {}  ({})", sink.name, sink.description);
        if let Some(monitor) = sink.monitor_source_name {
            println!("    monitor: {}{}", monitor, marker);
        }
    }
    Ok(())
}

pub fn run_cleanup() -> Result<()> {
    let runner = RealPactl;
    let count = audio::cleanup_orphans(&runner)?;
    if count == 0 {
        println!("No orphaned meetmix modules found.");
    } else {
        println!("Cleaned up {} orphaned module(s).", count);
    }
    Ok(())
}

fn setup_pipeline(
    runner: &dyn PactlRunner,
    session: &mut Session,
    device_match: &str,
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    audio::cleanup_orphans(runner)?;

    session.device_match = Some(device_match.to_string());
    session.original_default_sink = audio::default_sink(runner)?;
    if let Some(default_sink) = &session.original_default_sink {
        session.original_bt_sink_volume = audio::save_sink_volume(runner, default_sink);
    }
    if let Some(card) = audio::find_bt_card(runner, device_match)? {
        session.original_card_profile = Some(card.active_profile);
    }

    audio::disable_wpctl_autoswitch();
    logger.log("Disabled WirePlumber BT profile auto-switch")?;

    let mut profile_messages = Vec::new();
    audio::ensure_headset_profile(
        runner,
        device_match,
        || signals.stop_requested(),
        &mut profile_messages,
    )?;
    for (is_warning, message) in profile_messages {
        if is_warning {
            logger.warn(message)?;
        } else {
            logger.log(message)?;
        }
    }

    let combined = audio::create_combined_sink(runner)?;
    session
        .modules
        .push(("combined_sink".into(), combined.clone()));
    logger.log(format!(
        "Created null sink: {} (module {})",
        audio::NULL_SINK_NAME,
        combined
    ))?;
    runner.run(&["set-default-sink", audio::NULL_SINK_NAME])?;
    audio::move_sink_inputs(runner, audio::NULL_SINK_NAME)?;
    logger.log(format!(
        "Default sink: {} (was {})",
        audio::NULL_SINK_NAME,
        session
            .original_default_sink
            .as_deref()
            .unwrap_or("unknown")
    ))?;

    if signals.stop_requested() {
        return Ok(());
    }

    let mic_source = audio::find_mic_source(runner, device_match)?;
    let bt_sink = audio::find_bt_sink(runner, device_match)?;
    session.bt_sink_name = Some(bt_sink.name.clone());
    let bt_serial = audio::get_node_serial(&bt_sink.name);

    logger.log(format!("Mic source: {}", mic_source.name))?;
    logger.log(format!("Speaker sink: {}", bt_sink.name))?;

    if let Some(volume) = session.original_bt_sink_volume {
        audio::set_sink_volume(runner, &bt_sink.name, volume);
        logger.log(format!(
            "Speaker volume set: {}% on {}",
            volume, bt_sink.name
        ))?;
    }

    if mic_source.mute == Some(true) {
        logger.warn(
            "Selected mic source is muted; preserving mute state. Mic audio will be silent until unmuted.",
        )?;
    }
    audio::prepare_source(runner, &mic_source);
    logger.log(format!("Source volume: {}%", audio::MIC_VOLUME_PERCENT))?;

    audio::create_capture_devices(runner, &mic_source.name, &mut session.modules)?;
    logger.log(format!(
        "Created capture sink: {}",
        audio::CAPTURE_SINK_NAME
    ))?;
    logger.log("Loopback mic -> capture")?;

    if let Some(serial) = audio::get_node_serial(audio::CAPTURE_SINK_NAME) {
        session.capture_target = serial;
    }

    session.forwarding_loopback = Some(start_forwarding_loopback(
        bt_serial.as_deref().unwrap_or(&bt_sink.name),
        logger,
    )?);
    wait_for_forwarding_link(session, signals, logger, Duration::from_secs(5))?;
    warm_up_sco(session, signals, logger)?;
    session.capture_loopback = Some(start_capture_loopback(logger)?);

    thread::sleep(Duration::from_millis(500));
    verify_forwarding_link(runner, session, logger)?;
    Ok(())
}

fn run_recording(
    runner: &dyn PactlRunner,
    session: &mut Session,
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    let wav_path = session.wav_path.as_ref().context("missing WAV path")?;
    logger.log(format!("Recording to: {}", wav_path.display()))?;
    logger.log(format!(
        "Capturing from: {} (target {})",
        audio::CAPTURE_SINK_NAME,
        session.capture_target
    ))?;
    logger.log("Resume any paused media in your browser (the profile switch may pause it).")?;
    logger.log("Press Ctrl-C to stop recording and process with minutes.")?;

    let mut command = Command::new("pw-record");
    command
        .arg(format!("--target={}", session.capture_target))
        .args(["-P", "stream.capture.sink=true"])
        .arg(wav_path);
    apply_log_stdio(&mut command, logger)?;
    let child = command.spawn().context("starting pw-record")?;
    logger.log(format!("pw-record started (pid {})", child.id()))?;
    session.record_proc = Some(child);

    let mut monitor = RecordingMonitor::new(runner, session);
    let mut tick = 0_u32;
    let mut stop_recording = false;
    while let Some(proc) = session.record_proc.as_mut() {
        if let Some(status) = proc.try_wait()? {
            logger.log(format!("pw-record exited (code {:?})", status.code()))?;
            if !status.success() && !signals.stop_requested() {
                logger.warn(format!(
                    "Warning: pw-record exited unexpectedly (code {:?})",
                    status.code()
                ))?;
            }
            break;
        }
        if signals.stop_requested() {
            logger.log("Stop requested, waiting for pw-record...")?;
            stop_recording = true;
            break;
        }
        tick += 1;
        if tick % MONITOR_INTERVAL == 0 {
            monitor.check(runner, session, logger)?;
        }
        thread::sleep(Duration::from_millis(250));
    }
    if stop_recording {
        process::stop_child(&mut session.record_proc);
    }
    Ok(())
}

fn cleanup_session(
    runner: &dyn PactlRunner,
    session: &mut Session,
    logger: &mut Logger,
) -> Result<()> {
    process::stop_child(&mut session.record_proc);
    process::stop_child(&mut session.sco_warmup_proc);
    process::stop_child(&mut session.capture_loopback);
    process::stop_child(&mut session.forwarding_loopback);

    if let Some(default_sink) = &session.original_default_sink {
        let _ = runner.run_ok(&["set-default-sink", default_sink]);
    }
    audio::unload_modules(runner, &mut session.modules);
    restore_bt_profile(runner, session);
    audio::restore_wpctl_autoswitch();
    logger.log("Restored WirePlumber BT profile auto-switch")?;
    if let (Some(default_sink), Some(volume)) = (
        &session.original_default_sink,
        session.original_bt_sink_volume,
    ) {
        audio::set_sink_volume(runner, default_sink, volume);
    }
    Ok(())
}

fn process_recording(
    session: &mut Session,
    config: &Config,
    extra_args: Vec<String>,
    logger: &mut Logger,
) -> Result<i32> {
    let wav_path = session.wav_path.as_ref().context("missing WAV path")?;
    if !wav_path.exists() {
        bail!("No recording file produced.");
    }

    match wav::fix_wav_header(wav_path) {
        Ok(true) => logger.log("Fixed WAV header")?,
        Ok(false) => {}
        Err(err) => logger.warn(format!("Warning: Could not fix WAV header: {:#}", err))?,
    }
    let file_size = std::fs::metadata(wav_path)?.len();
    if file_size < 1000 {
        logger.warn(format!(
            "Warning: Recording file is very small ({} bytes).",
            file_size
        ))?;
    }
    match wav::analyze(wav_path) {
        Ok(stats) => {
            logger.log(format!(
                "WAV: {:.1}s, {}Hz, {}ch, {}-bit, {} bytes",
                stats.duration_seconds,
                stats.rate,
                stats.channels,
                stats.sample_width_bytes * 8,
                stats.file_size
            ))?;
            if let (Some(peak), Some(rms)) = (stats.peak, stats.rms) {
                logger.log(format!(
                    "WAV peak amplitude: {} (of 32768), RMS: {:.0}",
                    peak, rms
                ))?;
                if peak < 100 {
                    logger.warn("Warning: Recording appears silent (peak < 100).")?;
                }
            }
        }
        Err(err) => logger.warn(format!("Warning: Could not analyze WAV: {:#}", err))?,
    }
    logger.log("Processing with minutes...")?;

    let mut cmd = vec![
        "process".to_string(),
        "--content-type".to_string(),
        "meeting".to_string(),
        wav_path.display().to_string(),
    ];
    if let Some(language) = &config.language {
        cmd.extend(["--language".to_string(), language.clone()]);
    }
    cmd.extend(extra_args);

    logger.log(format!("Running: minutes {}", cmd.join(" ")))?;
    let code = run_minutes(&cmd, logger)?;
    if code == 0 && !session.keep_recording {
        let _ = std::fs::remove_file(wav_path);
    } else if code == 0 {
        logger.log(format!("Recording kept: {}", wav_path.display()))?;
    } else {
        logger.log(format!(
            "Recording kept (minutes exit code {}): {}",
            code,
            wav_path.display()
        ))?;
    }
    Ok(code)
}

fn run_minutes(args: &[String], logger: &mut Logger) -> Result<i32> {
    let mut proc = Command::new("minutes")
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .context("starting minutes process")?;
    if let Some(stdout) = proc.stdout.take() {
        for line in BufReader::new(stdout).lines() {
            let line = line?;
            println!("{}", line);
            logger.log_file_only(format!("[minutes] {}", line))?;
        }
    }
    let status = proc.wait()?;
    Ok(status.code().unwrap_or(1))
}

fn start_forwarding_loopback(bt_sink_target: &str, logger: &mut Logger) -> Result<Child> {
    let mut command = Command::new("pw-loopback");
    command
        .args(["-C", audio::NULL_SINK_NAME])
        .args(["--capture-props", "stream.capture.sink=true"])
        .args([
            "--playback-props",
            &format!("target.object={} media.role=Communication", bt_sink_target),
        ]);
    apply_log_stdio(&mut command, logger)?;
    let proc = command.spawn().context("starting forwarding pw-loopback")?;
    logger.log(format!(
        "Loopback combined -> speaker (pw-loopback pid {}, target {})",
        proc.id(),
        bt_sink_target
    ))?;
    Ok(proc)
}

fn start_capture_loopback(logger: &mut Logger) -> Result<Child> {
    let mut command = Command::new("pw-loopback");
    command
        .args(["-C", audio::NULL_SINK_NAME])
        .args(["--capture-props", "stream.capture.sink=true"])
        .args(["-P", audio::CAPTURE_SINK_NAME]);
    apply_log_stdio(&mut command, logger)?;
    let proc = command.spawn().context("starting capture pw-loopback")?;
    logger.log(format!(
        "Loopback combined -> capture (pw-loopback pid {})",
        proc.id()
    ))?;
    Ok(proc)
}

fn warm_up_sco(
    session: &mut Session,
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    if signals.stop_requested() {
        return Ok(());
    }
    let mut command = Command::new("pw-play");
    command
        .arg(format!("--target={}", audio::NULL_SINK_NAME))
        .args(["--format=s16", "--rate=48000", "--channels=2", "/dev/zero"])
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let child = command.spawn().context("starting SCO warmup")?;
    logger.log(format!(
        "SCO warmup started (pid {}, {}s)",
        child.id(),
        SCO_WARMUP_SECONDS
    ))?;
    session.sco_warmup_proc = Some(child);
    let deadline = Instant::now() + Duration::from_secs(SCO_WARMUP_SECONDS);
    while Instant::now() < deadline && !signals.stop_requested() {
        thread::sleep(Duration::from_millis(250));
    }
    process::stop_child(&mut session.sco_warmup_proc);
    logger.log("SCO warmup complete")?;
    Ok(())
}

fn wait_for_forwarding_link(
    session: &mut Session,
    signals: &signals::Handles,
    logger: &mut Logger,
    timeout: Duration,
) -> Result<()> {
    let Some(proc) = session.forwarding_loopback.as_mut() else {
        return Ok(());
    };
    let Some(bt_sink_name) = session.bt_sink_name.as_deref() else {
        return Ok(());
    };
    let loopback_id = format!("pw-loopback-{}", proc.id());
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline && !signals.stop_requested() {
        if let Some(status) = proc.try_wait()? {
            logger.warn(format!(
                "Warning: forwarding pw-loopback exited (code {:?})",
                status.code()
            ))?;
            return Ok(());
        }
        if let Ok(output) = Command::new("pw-link").arg("-l").output() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            if audio::check_loopback_linked(&stdout, &loopback_id, bt_sink_name) {
                logger.log(format!("Forwarding linked to {}", bt_sink_name))?;
                return Ok(());
            }
        }
        thread::sleep(Duration::from_millis(250));
    }
    logger.warn(format!(
        "Warning: forwarding loopback not linked after {}s, proceeding anyway",
        timeout.as_secs()
    ))?;
    Ok(())
}

fn verify_forwarding_link(
    runner: &dyn PactlRunner,
    session: &mut Session,
    logger: &mut Logger,
) -> Result<()> {
    if let Some(bt_sink_name) = &session.bt_sink_name {
        if let Ok(Some(sink)) = audio::list_sinks(runner)
            .map(|sinks| sinks.into_iter().find(|sink| sink.name == *bt_sink_name))
        {
            logger.log(format!(
                "BT sink: {} state={}",
                bt_sink_name,
                sink.state.unwrap_or_else(|| "unknown".into())
            ))?;
        }
    }
    Ok(())
}

fn restore_bt_profile(runner: &dyn PactlRunner, session: &Session) {
    let (Some(profile), Some(device_match)) = (
        session.original_card_profile.as_deref(),
        session.device_match.as_deref(),
    ) else {
        return;
    };
    if let Ok(Some(card)) = audio::find_bt_card(runner, device_match) {
        if card.active_profile != profile {
            let _ = runner.run_ok(&["set-card-profile", &card.name, profile]);
        }
    }
    if let Some(default_sink) = &session.original_default_sink {
        let pre_default = audio::default_sink(runner).ok().flatten();
        if pre_default.as_deref() == Some(default_sink) {
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline {
                if audio::default_sink(runner).ok().flatten().as_deref() != Some(default_sink) {
                    break;
                }
                thread::sleep(Duration::from_millis(240));
            }
        }
        let _ = runner.run_ok(&["set-default-sink", default_sink]);
    }
}

struct RecordingMonitor {
    last_profile: Option<String>,
    last_default_sink: Option<String>,
    loopback_warned: bool,
    fwd_warned: bool,
}

impl RecordingMonitor {
    fn new(runner: &dyn PactlRunner, session: &Session) -> Self {
        let last_profile = session
            .device_match
            .as_deref()
            .and_then(|pattern| audio::find_bt_card(runner, pattern).ok().flatten())
            .map(|card| card.active_profile);
        let last_default_sink = audio::default_sink(runner).ok().flatten();
        Self {
            last_profile,
            last_default_sink,
            loopback_warned: false,
            fwd_warned: false,
        }
    }

    fn check(
        &mut self,
        runner: &dyn PactlRunner,
        session: &mut Session,
        logger: &mut Logger,
    ) -> Result<()> {
        if let Some(pattern) = &session.device_match {
            if let Ok(Some(card)) = audio::find_bt_card(runner, pattern) {
                if Some(card.active_profile.clone()) != self.last_profile {
                    if let Some(last) = &self.last_profile {
                        logger.warn(format!(
                            "BT profile changed: {} -> {}",
                            last, card.active_profile
                        ))?;
                    }
                    self.last_profile = Some(card.active_profile);
                }
            }
        }
        if !self.loopback_warned {
            if let Some(proc) = session.capture_loopback.as_mut() {
                if let Some(status) = proc.try_wait()? {
                    logger.warn(format!(
                        "pw-loopback (capture) exited unexpectedly (code {:?})",
                        status.code()
                    ))?;
                    self.loopback_warned = true;
                }
            }
        }
        if !self.fwd_warned {
            if let Some(proc) = session.forwarding_loopback.as_mut() {
                if let Some(status) = proc.try_wait()? {
                    logger.warn(format!(
                        "pw-loopback (forwarding) exited unexpectedly (code {:?})",
                        status.code()
                    ))?;
                    self.fwd_warned = true;
                }
            }
        }
        if let Ok(default_sink) = audio::default_sink(runner) {
            if default_sink != self.last_default_sink {
                logger.warn(format!(
                    "Default sink changed: {} -> {}",
                    self.last_default_sink.as_deref().unwrap_or("unknown"),
                    default_sink.as_deref().unwrap_or("unknown")
                ))?;
                self.last_default_sink = default_sink;
            }
        }
        Ok(())
    }
}

fn apply_log_stdio(command: &mut Command, logger: &Logger) -> Result<()> {
    if let Some(file) = logger.clone_file()? {
        command.stdout(Stdio::from(file.try_clone()?));
        command.stderr(Stdio::from(file));
    } else {
        command.stdout(Stdio::null());
        command.stderr(Stdio::null());
    }
    Ok(())
}

#[allow(dead_code)]
fn _open_log_for_tests(path: PathBuf) -> Result<Logger> {
    Logger::new(&path)
}

#[allow(dead_code)]
fn _null_logger_for_tests() -> Logger {
    Logger::null()
}

#[allow(dead_code)]
fn _file_for_tests(path: PathBuf) -> Result<File> {
    Ok(File::open(path)?)
}
