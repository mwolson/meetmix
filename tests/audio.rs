use meetmix::audio::{
    check_loopback_linked, matches_device, parse_cards, parse_devices, parse_first_percent,
    parse_modules, AudioDevice, DeviceKind,
};

const SOURCES: &str = "Source #42
\tState: RUNNING
\tName: bluez_input.aa_bb
\tDescription: AirPods Pro
\tMute: yes
\tMonitor of Sink: n/a
Source #43
\tName: bluez_output.aa_bb.monitor
\tDescription: Monitor of AirPods Pro
\tMonitor of Sink: 12
";

const SINKS: &str = "Sink #12
\tState: IDLE
\tName: bluez_output.aa_bb
\tDescription: AirPods Pro
\tMute: no
\tMonitor Source: bluez_output.aa_bb.monitor
";

const CARDS: &str = "Card #5
\tName: bluez_card.aa_bb
\tProfiles:
\t\ta2dp-sink: High Fidelity Playback (available: yes)
\t\theadset-head-unit: Headset Head Unit (available: no)
\t\theadset-head-unit-msbc: Headset Head Unit (available: yes)
\tActive Profile: a2dp-sink
\tProperties:
\t\tdevice.description = \"AirPods Pro\"
";

const MODULES: &str = "Module #10
\tName: module-null-sink
\tArgument: sink_name=meetmix_combined
Module #11
\tName: module-null-sink
\tArgument: sink_name=other
";

#[test]
fn parse_sources_excludes_monitor_by_field() {
    let sources = parse_devices(SOURCES, DeviceKind::Source).unwrap();
    assert_eq!(sources.len(), 2);
    assert_eq!(sources[0].name, "bluez_input.aa_bb");
    assert_eq!(sources[0].monitor_of_sink, None);
    assert_eq!(sources[0].mute, Some(true));
    assert_eq!(sources[1].monitor_of_sink.as_deref(), Some("12"));
}

#[test]
fn parse_sinks_reads_monitor_source_and_state() {
    let sinks = parse_devices(SINKS, DeviceKind::Sink).unwrap();
    assert_eq!(sinks[0].name, "bluez_output.aa_bb");
    assert_eq!(
        sinks[0].monitor_source_name.as_deref(),
        Some("bluez_output.aa_bb.monitor")
    );
    assert_eq!(sinks[0].state.as_deref(), Some("idle"));
}

#[test]
fn parse_cards_reads_profiles_and_properties() {
    let cards = parse_cards(CARDS);
    assert_eq!(cards[0].name, "bluez_card.aa_bb");
    assert_eq!(cards[0].description, "AirPods Pro");
    assert_eq!(cards[0].active_profile, "a2dp-sink");
    assert!(!cards[0].profiles[1].available);
    assert!(cards[0].profiles[2].available);
}

#[test]
fn parse_modules_reads_arguments() {
    let modules = parse_modules(MODULES);
    assert_eq!(modules[0].index, "10");
    assert_eq!(modules[0].argument, "sink_name=meetmix_combined");
}

#[test]
fn matches_device_case_insensitive() {
    let device = AudioDevice {
        index: "1".into(),
        name: "bluez_input.airpods".into(),
        description: "Other".into(),
        monitor_of_sink: None,
        monitor_source_name: None,
        mute: None,
        state: None,
    };
    assert!(matches_device(&device, "AirPods"));
    assert!(!matches_device(&device, "Jabra"));
}

#[test]
fn check_loopback_linked_detects_forward_and_reverse_links() {
    assert!(check_loopback_linked(
        "pw-loopback-555:output_FL:\n   |-> bluez_output.abc:playback_FL\n",
        "pw-loopback-555",
        "bluez_output.abc"
    ));
    assert!(check_loopback_linked(
        "bluez_output.abc:playback_FL:\n   |<- pw-loopback-555:output_FL\n",
        "pw-loopback-555",
        "bluez_output.abc"
    ));
    assert!(!check_loopback_linked(
        "pw-loopback-555:output_FL:\n   |-> alsa_output.hdmi:playback_FL\n",
        "pw-loopback-555",
        "bluez_output.abc"
    ));
}

#[test]
fn parse_first_percent_reads_volume() {
    assert_eq!(
        parse_first_percent("Volume: front-left: 40000 / 61%"),
        Some(61)
    );
    assert_eq!(parse_first_percent("Mute: no"), None);
}
