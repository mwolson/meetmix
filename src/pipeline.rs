use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use chrono::Local;

use crate::audio::{self, PactlRunner, RealPactl};
use crate::cli::RecordBackend;
use crate::config::Config;
use crate::logging::{self, Logger};
use crate::process;
use crate::signals;
use crate::wav;

const MONITOR_INTERVAL: u32 = 8;
const SCO_WARMUP_SECONDS: u64 = 3;
const MINUTES_STOP_GRACE: Duration = Duration::from_secs(3);

pub struct Session {
    pub bt_sink_name: Option<String>,
    pub capture_loopback: Option<Child>,
    pub capture_target: String,
    pub device_match: Option<String>,
    pub forwarding_loopback: Option<Child>,
    pub keep_recording: bool,
    pub live_transcript: bool,
    pub live_proc: Option<Child>,
    pub log_path: Option<PathBuf>,
    pub modules: Vec<(String, String)>,
    pub original_bt_sink_volume: Option<u32>,
    pub original_card_profile: Option<String>,
    pub original_default_sink: Option<String>,
    pub record_backend: RecordBackend,
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
            live_transcript: true,
            live_proc: None,
            log_path: None,
            modules: Vec::new(),
            original_bt_sink_volume: None,
            original_card_profile: None,
            original_default_sink: None,
            record_backend: RecordBackend::PwRecord,
            record_proc: None,
            sco_warmup_proc: None,
            wav_path: None,
        }
    }
}

pub fn run_record(
    config: Config,
    keep_recording: bool,
    record_backend: RecordBackend,
    live_transcript: bool,
    extra_args: Vec<String>,
) -> Result<i32> {
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
        live_transcript,
        log_path: Some(log_path),
        record_backend,
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
            run_recording(&mut session, &config, &extra_args, &signals, &mut logger)
        }
        Ok(()) => {
            logger.log("Stop requested during setup, skipping recording.")?;
            Ok(())
        }
        Err(err) => Err(err),
    };

    cleanup_session(&runner, &mut session, &mut logger)?;
    record_result?;

    if recording_ran && session.record_backend == RecordBackend::PwRecord {
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
    session: &mut Session,
    config: &Config,
    extra_args: &[String],
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    match session.record_backend {
        RecordBackend::Minutes => {
            run_minutes_recording(session, config, extra_args, signals, logger)
        }
        RecordBackend::PwRecord => run_pw_recording(session, config, signals, logger),
    }
}

fn run_minutes_recording(
    session: &mut Session,
    config: &Config,
    extra_args: &[String],
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    logger.log("Recording with: minutes record (cpal)")?;
    logger.log(format!(
        "Capturing from Minutes device: {}",
        audio::CAPTURE_SINK_DESCRIPTION
    ))?;
    logger.log("Live transcript: enabled by Minutes recording sidecar")?;
    logger.log("Resume any paused media in your browser (the profile switch may pause it).")?;
    logger.log("Press Ctrl-C to stop recording and queue processing with minutes.")?;
    logger.log("Minutes will preserve the raw WAV next to the final meeting notes.")?;

    let mut command = Command::new("minutes");
    command
        .args([
            "record",
            "--device",
            audio::CAPTURE_SINK_DESCRIPTION,
            "--intent",
            "room",
        ])
        .args(extra_args);
    if let Some(language) = &config.language {
        command.args(["--language", language]);
    }
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());
    let child = command.spawn().context("starting minutes record")?;
    logger.log(format!("minutes record started (pid {})", child.id()))?;
    session.record_proc = Some(child);

    let live_stop = Arc::new(AtomicBool::new(false));
    let live_handle = start_live_transcript_printer(signals.clone(), live_stop.clone(), logger)?;
    let child_output;
    if let Some(proc) = session.record_proc.as_mut() {
        child_output = capture_child_output(proc, "minutes record");
    } else {
        stop_live_transcript_printer(live_stop, live_handle);
        return Ok(());
    }

    while let Some(proc) = session.record_proc.as_mut() {
        drain_child_output(&child_output, logger)?;
        if let Some(status) = proc.try_wait()? {
            drain_remaining_child_output(&child_output, logger)?;
            logger.log(format!(
                "minutes record exited ({})",
                format_exit_status(&status)
            ))?;
            if !status.success() && !signals.stop_requested() {
                logger.warn(format!(
                    "Warning: minutes record exited unexpectedly ({})",
                    format_exit_status(&status)
                ))?;
            }
            session.record_proc = None;
            stop_live_transcript_printer(live_stop, live_handle);
            return Ok(());
        }
        if signals.stop_requested() {
            logger.log("Stop requested, waiting for minutes record to finish...")?;
            if wait_for_child_exit(proc, MINUTES_STOP_GRACE, &child_output, logger)?.is_some() {
                session.record_proc = None;
                stop_live_transcript_printer(live_stop, live_handle);
                return Ok(());
            } else {
                logger.log("minutes record still running, asking minutes to stop recording...")?;
                stop_minutes_recording(logger)?;
            }
            break;
        }
        thread::sleep(Duration::from_millis(250));
    }

    if let Some(proc) = session.record_proc.as_mut() {
        let deadline = Instant::now() + Duration::from_secs(30);
        while Instant::now() < deadline {
            drain_child_output(&child_output, logger)?;
            if let Some(status) = proc.try_wait()? {
                drain_remaining_child_output(&child_output, logger)?;
                logger.log(format!(
                    "minutes record exited ({})",
                    format_exit_status(&status)
                ))?;
                session.record_proc = None;
                stop_live_transcript_printer(live_stop, live_handle);
                return Ok(());
            }
            thread::sleep(Duration::from_millis(250));
        }
        stop_live_transcript_printer(live_stop, live_handle);
        logger.warn("Warning: minutes record did not exit within 30s after stop request.")?;
    }
    Ok(())
}

fn run_pw_recording(
    session: &mut Session,
    config: &Config,
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    let wav_path = session
        .wav_path
        .as_ref()
        .context("missing WAV path")?
        .clone();
    logger.log("Recording with: pw-record")?;
    logger.log(format!("Recording to: {}", wav_path.display()))?;
    logger.log(format!(
        "Capturing from: {} (target {})",
        audio::CAPTURE_SINK_NAME,
        session.capture_target
    ))?;
    let live_stop = Arc::new(AtomicBool::new(false));
    let live_handle = if session.live_transcript {
        start_minutes_live(session, config, signals, live_stop.clone(), logger)?
    } else {
        logger.log("Live transcript: disabled")?;
        None
    };
    logger.log("Resume any paused media in your browser (the profile switch may pause it).")?;
    logger.log("Press Ctrl-C to stop recording and process with minutes.")?;

    let mut command = Command::new("pw-record");
    command
        .arg(format!("--target={}", session.capture_target))
        .args(["-P", "stream.capture.sink=true"])
        .arg(&wav_path);
    apply_log_stdio(&mut command, logger)?;
    let child = command.spawn().context("starting pw-record")?;
    logger.log(format!("pw-record started (pid {})", child.id()))?;
    session.record_proc = Some(child);

    let live_output = session
        .live_proc
        .as_mut()
        .map(|proc| capture_child_output(proc, "minutes live"));
    let runner = RealPactl;
    let mut monitor = RecordingMonitor::new(&runner, session);
    let mut tick = 0_u32;
    let mut stop_recording = false;
    while session.record_proc.is_some() {
        if let Some(output) = &live_output {
            drain_child_output(output, logger)?;
        }
        check_live_process(session, &live_output, signals, logger)?;
        let status = if let Some(proc) = session.record_proc.as_mut() {
            proc.try_wait()?
        } else {
            None
        };
        if let Some(status) = status {
            logger.log(format!(
                "pw-record exited ({})",
                format_exit_status(&status)
            ))?;
            if !status.success() && !signals.stop_requested() {
                logger.warn(format!(
                    "Warning: pw-record exited unexpectedly ({})",
                    format_exit_status(&status)
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
            monitor.check(&runner, session, logger)?;
        }
        thread::sleep(Duration::from_millis(250));
    }
    if stop_recording {
        process::stop_child(&mut session.record_proc);
    }
    stop_minutes_live(
        session,
        live_output.as_ref(),
        live_stop,
        live_handle,
        logger,
    )?;
    Ok(())
}

fn start_minutes_live(
    session: &mut Session,
    config: &Config,
    signals: &signals::Handles,
    live_stop: Arc<AtomicBool>,
    logger: &mut Logger,
) -> Result<Option<thread::JoinHandle<()>>> {
    logger.log("Live transcript: minutes live")?;
    logger.log(format!(
        "Live transcript device: {}",
        audio::CAPTURE_SINK_DESCRIPTION
    ))?;

    let mut command = Command::new("minutes");
    command
        .args(["live", "--device", audio::CAPTURE_SINK_DESCRIPTION])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(language) = &config.language {
        command.args(["--language", language]);
    }
    let child = command.spawn().context("starting minutes live")?;
    logger.log(format!("minutes live started (pid {})", child.id()))?;
    session.live_proc = Some(child);
    Ok(Some(start_live_transcript_printer(
        signals.clone(),
        live_stop,
        logger,
    )?))
}

fn check_live_process(
    session: &mut Session,
    output: &Option<ChildOutput>,
    signals: &signals::Handles,
    logger: &mut Logger,
) -> Result<()> {
    let Some(proc) = session.live_proc.as_mut() else {
        return Ok(());
    };
    if let Some(status) = proc.try_wait()? {
        if let Some(output) = output {
            drain_remaining_child_output(output, logger)?;
        }
        logger.log(format!(
            "minutes live exited ({})",
            format_exit_status(&status)
        ))?;
        if !status.success() && !signals.stop_requested() {
            logger.warn(format!(
                "Warning: minutes live exited unexpectedly ({}). Continuing final recording.",
                format_exit_status(&status)
            ))?;
        }
        session.live_proc = None;
    }
    Ok(())
}

fn stop_minutes_live(
    session: &mut Session,
    output: Option<&ChildOutput>,
    live_stop: Arc<AtomicBool>,
    live_handle: Option<thread::JoinHandle<()>>,
    logger: &mut Logger,
) -> Result<()> {
    let Some(proc) = session.live_proc.as_mut() else {
        if let Some(handle) = live_handle {
            stop_live_transcript_printer(live_stop, handle);
        }
        return Ok(());
    };

    if proc.try_wait()?.is_none() {
        logger.log("Stopping live transcript...")?;
        stop_minutes(logger)?;
        if let Some(output) = output {
            if wait_for_child_exit(proc, MINUTES_STOP_GRACE, output, logger)?.is_none() {
                process::stop_child(&mut session.live_proc);
            }
        } else {
            process::stop_child(&mut session.live_proc);
        }
    }
    if let Some(output) = output {
        drain_remaining_child_output(output, logger)?;
    }
    session.live_proc = None;
    if let Some(handle) = live_handle {
        stop_live_transcript_printer(live_stop, handle);
    }
    Ok(())
}

fn stop_minutes_recording(logger: &mut Logger) -> Result<()> {
    stop_minutes(logger)
}

fn stop_minutes(logger: &mut Logger) -> Result<()> {
    let output = Command::new("minutes")
        .arg("stop")
        .output()
        .context("running minutes stop")?;
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        logger.log_file_only(format!("[minutes stop] {}", line))?;
    }
    for line in String::from_utf8_lossy(&output.stderr).lines() {
        logger.log_file_only(format!("[minutes stop] {}", line))?;
    }
    if !output.status.success() {
        logger.warn(format!(
            "Warning: minutes stop exited with code {:?}",
            output.status.code()
        ))?;
    }
    Ok(())
}

#[derive(Copy, Clone)]
enum ChildStream {
    Stdout,
    Stderr,
}

struct ChildOutput {
    label: &'static str,
    rx: mpsc::Receiver<(ChildStream, String)>,
}

fn capture_child_output(child: &mut Child, label: &'static str) -> ChildOutput {
    let (tx, rx) = mpsc::channel();
    if let Some(stdout) = child.stdout.take() {
        read_child_lines(stdout, ChildStream::Stdout, tx.clone());
    }
    if let Some(stderr) = child.stderr.take() {
        read_child_lines(stderr, ChildStream::Stderr, tx.clone());
    }
    drop(tx);
    ChildOutput { label, rx }
}

fn read_child_lines<R: Read + Send + 'static>(
    reader: R,
    stream: ChildStream,
    tx: mpsc::Sender<(ChildStream, String)>,
) {
    thread::spawn(move || {
        for line in BufReader::new(reader).lines().map_while(Result::ok) {
            if tx.send((stream, line)).is_err() {
                break;
            }
        }
    });
}

fn drain_child_output(output: &ChildOutput, logger: &mut Logger) -> Result<()> {
    while let Ok((stream, line)) = output.rx.try_recv() {
        emit_child_line(output.label, stream, &line, logger)?;
    }
    Ok(())
}

fn drain_remaining_child_output(output: &ChildOutput, logger: &mut Logger) -> Result<()> {
    while let Ok((stream, line)) = output.rx.recv_timeout(Duration::from_millis(50)) {
        emit_child_line(output.label, stream, &line, logger)?;
    }
    Ok(())
}

fn emit_child_line(
    label: &str,
    stream: ChildStream,
    line: &str,
    logger: &mut Logger,
) -> Result<()> {
    match stream {
        ChildStream::Stdout => println!("{}", line),
        ChildStream::Stderr => eprintln!("{}", line),
    }
    logger.log_file_only(format!("[{}] {}", label, line))?;
    Ok(())
}

fn wait_for_child_exit(
    proc: &mut Child,
    timeout: Duration,
    output: &ChildOutput,
    logger: &mut Logger,
) -> Result<Option<ExitStatus>> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        drain_child_output(output, logger)?;
        if let Some(status) = proc.try_wait()? {
            drain_remaining_child_output(output, logger)?;
            logger.log(format!(
                "{} exited ({})",
                output.label,
                format_exit_status(&status)
            ))?;
            return Ok(Some(status));
        }
        thread::sleep(Duration::from_millis(100));
    }
    Ok(None)
}

fn format_exit_status(status: &ExitStatus) -> String {
    if let Some(code) = status.code() {
        format!("exit code {}", code)
    } else {
        "terminated by signal".to_string()
    }
}

fn start_live_transcript_printer(
    signals: signals::Handles,
    stop: Arc<AtomicBool>,
    logger: &Logger,
) -> Result<thread::JoinHandle<()>> {
    let log_file = logger.clone_file()?;
    Ok(thread::spawn(move || {
        let Some(path) = std::env::var_os("HOME")
            .map(PathBuf::from)
            .map(|home| home.join(".minutes/live-transcript.jsonl"))
        else {
            return;
        };
        let mut offset = std::fs::metadata(&path)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        while !stop.load(Ordering::SeqCst) && !signals.stop_requested() {
            if let Ok(metadata) = std::fs::metadata(&path) {
                if metadata.len() < offset {
                    offset = 0;
                }
                if metadata.len() > offset {
                    if let Ok(mut file) = File::open(&path) {
                        use std::io::{Seek, SeekFrom};
                        if file.seek(SeekFrom::Start(offset)).is_ok() {
                            let mut reader = BufReader::new(file);
                            let mut line = String::new();
                            loop {
                                line.clear();
                                let Ok(read) = reader.read_line(&mut line) else {
                                    break;
                                };
                                if read == 0 {
                                    break;
                                }
                                offset += read as u64;
                                if let Some(text) = live_text_from_json(line.trim_end()) {
                                    eprintln!("[live] {}", text);
                                    if let Some(mut file) =
                                        log_file.as_ref().and_then(|file| file.try_clone().ok())
                                    {
                                        let _ = writeln!(
                                            file,
                                            "[{}] [live] {}",
                                            Local::now().format("%H:%M:%S"),
                                            text
                                        );
                                    }
                                }
                            }
                        }
                    }
                }
            }
            thread::sleep(Duration::from_millis(250));
        }
    }))
}

fn stop_live_transcript_printer(stop: Arc<AtomicBool>, handle: thread::JoinHandle<()>) {
    stop.store(true, Ordering::SeqCst);
    let _ = handle.join();
}

fn live_text_from_json(line: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    let text = value.get("text")?.as_str()?.trim();
    if text.is_empty() {
        None
    } else {
        Some(text.to_string())
    }
}

fn cleanup_session(
    runner: &dyn PactlRunner,
    session: &mut Session,
    logger: &mut Logger,
) -> Result<()> {
    process::stop_child(&mut session.record_proc);
    process::stop_child(&mut session.live_proc);
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
