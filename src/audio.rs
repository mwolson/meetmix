use std::process::Command;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use serde_json::Value;

pub const CAPTURE_SINK_DESCRIPTION: &str = "MeetMixCapture";
pub const CAPTURE_SINK_NAME: &str = "meetmix_capture";
pub const HEADSET_PROFILES: &[&str] = &["headset-head-unit", "headset-head-unit-msbc"];
pub const MIC_VOLUME_PERCENT: u32 = 100;
pub const MODULE_PREFIX: &str = "meetmix_";
pub const NULL_SINK_DESCRIPTION: &str = "MeetMixCombined";
pub const NULL_SINK_NAME: &str = "meetmix_combined";
pub const WPCTL_AUTOSWITCH_KEY: &str = "bluetooth.autoswitch-to-headset-profile";

pub trait PactlRunner {
    fn run(&self, args: &[&str]) -> Result<String>;
    fn run_ok(&self, args: &[&str]) -> String;
}

pub struct RealPactl;

impl PactlRunner for RealPactl {
    fn run(&self, args: &[&str]) -> Result<String> {
        let output = Command::new("pactl")
            .args(args)
            .output()
            .with_context(|| format!("running pactl {}", args.join(" ")))?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(anyhow!(
                "pactl {} failed: {}",
                args.join(" "),
                stderr.trim()
            ));
        }
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    }

    fn run_ok(&self, args: &[&str]) -> String {
        match Command::new("pactl").args(args).output() {
            Ok(out) => String::from_utf8_lossy(&out.stdout).into_owned(),
            Err(_) => String::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioDevice {
    pub index: String,
    pub name: String,
    pub description: String,
    pub monitor_of_sink: Option<String>,
    pub monitor_source_name: Option<String>,
    pub mute: Option<bool>,
    pub state: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Card {
    pub name: String,
    pub description: String,
    pub active_profile: String,
    pub profiles: Vec<Profile>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Profile {
    pub name: String,
    pub available: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Module {
    pub index: String,
    pub name: String,
    pub argument: String,
}

pub fn default_sink(runner: &dyn PactlRunner) -> Result<Option<String>> {
    let out = runner.run(&["info"])?;
    Ok(out
        .lines()
        .find_map(|line| line.strip_prefix("Default Sink: "))
        .map(|value| value.trim().to_string()))
}

pub fn list_sources(runner: &dyn PactlRunner) -> Result<Vec<AudioDevice>> {
    parse_devices(&runner.run(&["list", "sources"])?, DeviceKind::Source)
}

pub fn list_sinks(runner: &dyn PactlRunner) -> Result<Vec<AudioDevice>> {
    parse_devices(&runner.run(&["list", "sinks"])?, DeviceKind::Sink)
}

pub fn list_cards(runner: &dyn PactlRunner) -> Result<Vec<Card>> {
    Ok(parse_cards(&runner.run(&["list", "cards"])?))
}

pub fn list_modules(runner: &dyn PactlRunner) -> Result<Vec<Module>> {
    Ok(parse_modules(&runner.run(&["list", "modules"])?))
}

pub fn find_bt_card(runner: &dyn PactlRunner, device_match: &str) -> Result<Option<Card>> {
    Ok(list_cards(runner)?.into_iter().find(|card| {
        matches_text(&card.name, device_match) || matches_text(&card.description, device_match)
    }))
}

pub fn find_mic_source(runner: &dyn PactlRunner, device_match: &str) -> Result<AudioDevice> {
    find_one(
        list_sources(runner)?
            .into_iter()
            .filter(|source| source.monitor_of_sink.is_none())
            .filter(|source| matches_device(source, device_match))
            .collect(),
        "microphone source",
        device_match,
    )
}

pub fn find_bt_sink(runner: &dyn PactlRunner, device_match: &str) -> Result<AudioDevice> {
    find_one(
        list_sinks(runner)?
            .into_iter()
            .filter(|sink| matches_device(sink, device_match))
            .collect(),
        "speaker sink",
        device_match,
    )
}

pub fn matches_device(device: &AudioDevice, pattern: &str) -> bool {
    matches_text(&device.name, pattern) || matches_text(&device.description, pattern)
}

pub fn ensure_headset_profile(
    runner: &dyn PactlRunner,
    device_match: &str,
    stop_requested: impl Fn() -> bool,
    messages: &mut Vec<(bool, String)>,
) -> Result<()> {
    let Some(card) = find_bt_card(runner, device_match)? else {
        return Ok(());
    };
    let active = card.active_profile.as_str();
    if active.starts_with("headset-head-unit") {
        messages.push((false, format!("BT profile: {}", active)));
        return Ok(());
    }

    for profile in HEADSET_PROFILES {
        if card
            .profiles
            .iter()
            .any(|p| p.name == *profile && p.available)
        {
            messages.push((
                false,
                format!("Switching BT profile from {} to {}", active, profile),
            ));
            runner.run(&["set-card-profile", &card.name, profile])?;
            if wait_for_sink(
                runner,
                device_match,
                Duration::from_secs(5),
                &stop_requested,
            )?
            .is_none()
                && !stop_requested()
            {
                messages.push((
                    true,
                    "Warning: HFP sink did not appear after profile switch".to_string(),
                ));
            }
            if wait_for_source(
                runner,
                device_match,
                Duration::from_secs(5),
                &stop_requested,
            )?
            .is_none()
                && !stop_requested()
            {
                messages.push((
                    true,
                    "Warning: HFP source did not appear after profile switch".to_string(),
                ));
            }
            return Ok(());
        }
    }

    messages.push((
        true,
        format!(
            "Warning: no headset profile available, staying on {}",
            active
        ),
    ));
    Ok(())
}

pub fn wait_for_sink(
    runner: &dyn PactlRunner,
    device_match: &str,
    timeout: Duration,
    stop_requested: impl Fn() -> bool,
) -> Result<Option<String>> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline && !stop_requested() {
        if let Ok(sink) = find_bt_sink(runner, device_match) {
            return Ok(Some(sink.name));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Ok(None)
}

pub fn wait_for_source(
    runner: &dyn PactlRunner,
    device_match: &str,
    timeout: Duration,
    stop_requested: impl Fn() -> bool,
) -> Result<Option<String>> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline && !stop_requested() {
        if let Ok(source) = find_mic_source(runner, device_match) {
            return Ok(Some(source.name));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Ok(None)
}

pub fn save_sink_volume(runner: &dyn PactlRunner, sink_name: &str) -> Option<u32> {
    parse_first_percent(&runner.run_ok(&["get-sink-volume", sink_name]))
}

pub fn set_sink_volume(runner: &dyn PactlRunner, sink_name: &str, volume: u32) {
    let volume_arg = format!("{}%", volume);
    let _ = runner.run_ok(&["set-sink-volume", sink_name, &volume_arg]);
}

pub fn prepare_source(runner: &dyn PactlRunner, source: &AudioDevice) {
    let volume = format!("{}%", MIC_VOLUME_PERCENT);
    let _ = runner.run_ok(&["set-source-volume", &source.name, &volume]);
}

pub fn create_combined_sink(runner: &dyn PactlRunner) -> Result<String> {
    load_module(
        runner,
        "module-null-sink",
        &[
            &format!("sink_name={}", NULL_SINK_NAME),
            &format!(
                "sink_properties=device.description={}",
                NULL_SINK_DESCRIPTION
            ),
        ],
    )
}

pub fn create_capture_devices(
    runner: &dyn PactlRunner,
    mic_source: &str,
    modules: &mut Vec<(String, String)>,
) -> Result<()> {
    let capture = load_module(
        runner,
        "module-null-sink",
        &[
            &format!("sink_name={}", CAPTURE_SINK_NAME),
            &format!(
                "sink_properties=device.description={}",
                CAPTURE_SINK_DESCRIPTION
            ),
        ],
    )?;
    modules.push(("capture_sink".into(), capture));

    let loopback = load_module(
        runner,
        "module-loopback",
        &[
            &format!("source={}", mic_source),
            &format!("sink={}", CAPTURE_SINK_NAME),
            "latency_msec=1",
            "source_output_properties=media.role=Communication",
        ],
    )?;
    modules.push(("mic_loopback".into(), loopback));
    Ok(())
}

pub fn move_sink_inputs(runner: &dyn PactlRunner, target_sink_name: &str) -> Result<()> {
    let Some(target) = list_sinks(runner)?
        .into_iter()
        .find(|sink| sink.name == target_sink_name)
    else {
        return Ok(());
    };
    let out = runner.run(&["list", "short", "sink-inputs"])?;
    for line in out.lines() {
        let fields: Vec<&str> = line.split('\t').collect();
        let Some(input_idx) = fields.first() else {
            continue;
        };
        if fields.get(1) == Some(&target.index.as_str()) {
            continue;
        }
        let _ = runner.run_ok(&["move-sink-input", input_idx, target_sink_name]);
    }
    Ok(())
}

pub fn cleanup_orphans(runner: &dyn PactlRunner) -> Result<usize> {
    let mut count = 0;
    for module in list_modules(runner)? {
        if module.argument.contains(MODULE_PREFIX) {
            let _ = runner.run_ok(&["unload-module", &module.index]);
            count += 1;
        }
    }
    Ok(count)
}

pub fn unload_modules(runner: &dyn PactlRunner, modules: &mut Vec<(String, String)>) {
    for (_, idx) in modules.iter().rev() {
        let _ = runner.run_ok(&["unload-module", idx]);
    }
    modules.clear();
}

pub fn get_node_serial(node_name: &str) -> Option<String> {
    let output = Command::new("pw-dump").output().ok()?;
    let json: Value = serde_json::from_slice(&output.stdout).ok()?;
    json.as_array()?.iter().find_map(|node| {
        let props = node.get("info")?.get("props")?;
        if props.get("node.name")?.as_str()? == node_name {
            props.get("object.serial").map(|serial| match serial {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            })
        } else {
            None
        }
    })
}

pub fn disable_wpctl_autoswitch() {
    let _ = Command::new("wpctl")
        .args(["settings", WPCTL_AUTOSWITCH_KEY, "false"])
        .output();
}

pub fn restore_wpctl_autoswitch() {
    let _ = Command::new("wpctl")
        .args(["settings", WPCTL_AUTOSWITCH_KEY, "true"])
        .output();
}

pub fn parse_first_percent(text: &str) -> Option<u32> {
    let percent_re = regex::Regex::new(r"(\d+)%").expect("percent regex");
    percent_re
        .captures(text)
        .and_then(|caps| caps.get(1))
        .and_then(|m| m.as_str().parse().ok())
}

pub fn check_loopback_linked(pw_link_output: &str, loopback_id: &str, bt_sink_name: &str) -> bool {
    let mut current_port_owner = None;
    for line in pw_link_output.lines() {
        let stripped = line.trim();
        if stripped.is_empty() {
            continue;
        }
        if !line.starts_with(char::is_whitespace) {
            current_port_owner = stripped.split(':').next();
        } else if stripped.contains('|') {
            let linked_to = stripped
                .split('|')
                .nth(1)
                .unwrap_or("")
                .trim_start_matches("-> ")
                .trim_start_matches("<- ");
            let linked_owner = linked_to.split(':').next().unwrap_or("");
            if let Some(owner) = current_port_owner {
                if owner.contains(loopback_id) && linked_owner.contains(bt_sink_name) {
                    return true;
                }
                if owner.contains(bt_sink_name) && linked_owner.contains(loopback_id) {
                    return true;
                }
            }
        }
    }
    false
}

fn load_module(runner: &dyn PactlRunner, module: &str, args: &[&str]) -> Result<String> {
    let mut command = vec!["load-module", module];
    command.extend(args);
    Ok(runner.run(&command)?.trim().to_string())
}

fn find_one(candidates: Vec<AudioDevice>, kind: &str, pattern: &str) -> Result<AudioDevice> {
    match candidates.len() {
        0 => bail!("no {} matching '{}' found", kind, pattern),
        1 => Ok(candidates.into_iter().next().expect("single candidate")),
        _ => bail!(
            "multiple {}s match '{}'; use a more specific --device-match",
            kind,
            pattern
        ),
    }
}

fn matches_text(value: &str, pattern: &str) -> bool {
    value.to_lowercase().contains(&pattern.to_lowercase())
}

#[derive(Debug, Clone, Copy)]
pub enum DeviceKind {
    Sink,
    Source,
}

pub fn parse_devices(text: &str, kind: DeviceKind) -> Result<Vec<AudioDevice>> {
    let section_prefix = match kind {
        DeviceKind::Sink => "Sink #",
        DeviceKind::Source => "Source #",
    };
    let mut devices = Vec::new();
    let mut current: Option<AudioDevice> = None;
    for line in text.lines() {
        if let Some(index) = line.strip_prefix(section_prefix) {
            if let Some(device) = current.take() {
                devices.push(device);
            }
            current = Some(AudioDevice {
                index: index.trim().to_string(),
                name: String::new(),
                description: String::new(),
                monitor_of_sink: None,
                monitor_source_name: None,
                mute: None,
                state: None,
            });
            continue;
        }
        let Some(device) = current.as_mut() else {
            continue;
        };
        let trimmed = line.trim();
        if let Some(value) = trimmed.strip_prefix("Name: ") {
            device.name = value.trim().to_string();
        } else if let Some(value) = trimmed.strip_prefix("Description: ") {
            device.description = value.trim().to_string();
        } else if let Some(value) = trimmed.strip_prefix("Monitor of Sink: ") {
            if value.trim() != "n/a" {
                device.monitor_of_sink = Some(value.trim().to_string());
            }
        } else if let Some(value) = trimmed.strip_prefix("Monitor Source: ") {
            device.monitor_source_name = Some(value.trim().to_string());
        } else if let Some(value) = trimmed.strip_prefix("Mute: ") {
            device.mute = Some(value.trim() == "yes");
        } else if let Some(value) = trimmed.strip_prefix("State: ") {
            device.state = Some(value.trim().to_lowercase());
        }
    }
    if let Some(device) = current {
        devices.push(device);
    }
    Ok(devices)
}

pub fn parse_cards(text: &str) -> Vec<Card> {
    let mut cards = Vec::new();
    let mut current: Option<Card> = None;
    let mut in_profiles = false;
    let mut in_properties = false;

    for line in text.lines() {
        if line.starts_with("Card #") {
            if let Some(card) = current.take() {
                cards.push(card);
            }
            current = Some(Card {
                name: String::new(),
                description: String::new(),
                active_profile: String::new(),
                profiles: Vec::new(),
            });
            in_profiles = false;
            in_properties = false;
            continue;
        }
        let Some(card) = current.as_mut() else {
            continue;
        };
        let trimmed = line.trim();
        if let Some(value) = trimmed.strip_prefix("Name: ") {
            card.name = value.to_string();
        } else if trimmed == "Profiles:" {
            in_profiles = true;
            in_properties = false;
        } else if trimmed == "Properties:" {
            in_properties = true;
            in_profiles = false;
        } else if let Some(value) = trimmed.strip_prefix("Active Profile: ") {
            card.active_profile = value.to_string();
            in_profiles = false;
        } else if in_profiles {
            if let Some((profile, rest)) = trimmed.split_once(':') {
                let available = !rest.contains("available: no");
                card.profiles.push(Profile {
                    name: profile.to_string(),
                    available,
                });
            }
        } else if in_properties {
            for key in ["device.description", "device.alias"] {
                let prefix = format!("{} = ", key);
                if let Some(value) = trimmed.strip_prefix(&prefix) {
                    let value = value.trim_matches('"').to_string();
                    if !value.is_empty() {
                        card.description = value;
                    }
                    break;
                }
            }
        }
    }
    if let Some(card) = current {
        cards.push(card);
    }
    cards
}

pub fn parse_modules(text: &str) -> Vec<Module> {
    let mut modules = Vec::new();
    let mut current: Option<Module> = None;
    for line in text.lines() {
        if let Some(index) = line.strip_prefix("Module #") {
            if let Some(module) = current.take() {
                modules.push(module);
            }
            current = Some(Module {
                index: index.trim().to_string(),
                name: String::new(),
                argument: String::new(),
            });
            continue;
        }
        let Some(module) = current.as_mut() else {
            continue;
        };
        let trimmed = line.trim();
        if let Some(value) = trimmed.strip_prefix("Name: ") {
            module.name = value.to_string();
        } else if let Some(value) = trimmed.strip_prefix("Argument: ") {
            module.argument = value.to_string();
        }
    }
    if let Some(module) = current {
        modules.push(module);
    }
    modules
}
