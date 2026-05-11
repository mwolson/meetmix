use std::path::Path;

use meetmix::cli::Overrides;
use meetmix::config::{parse_conf, Config};
use tempfile::TempDir;

#[test]
fn parse_conf_accepts_values_blanks_and_comments() {
    let text = "\
# leading comment
--device-match=AirPods Pro

--language=en
";
    let entries = parse_conf(text, Path::new("meetmix.conf")).unwrap();
    assert_eq!(
        entries,
        vec![
            ("--device-match".into(), "AirPods Pro".into()),
            ("--language".into(), "en".into()),
        ]
    );
}

#[test]
fn parse_conf_rejects_malformed_line() {
    let err = parse_conf("--bare-flag\n", Path::new("meetmix.conf")).unwrap_err();
    assert!(format!("{}", err).contains("malformed"));
}

#[test]
fn config_build_applies_file_values() {
    let dir = TempDir::new().unwrap();
    let conf = dir.path().join("meetmix.conf");
    std::fs::write(&conf, "--device-match=AirPods\n--language=en\n").unwrap();
    let config = Config::build(&Overrides::default(), Some(&conf)).unwrap();
    assert_eq!(config.device_match.as_deref(), Some("AirPods"));
    assert_eq!(config.language.as_deref(), Some("en"));
}

#[test]
fn config_build_cli_overrides_file() {
    let dir = TempDir::new().unwrap();
    let conf = dir.path().join("meetmix.conf");
    std::fs::write(&conf, "--device-match=AirPods\n--language=en\n").unwrap();
    let overrides = Overrides {
        device_match: Some("Jabra".into()),
        language: None,
    };
    let config = Config::build(&overrides, Some(&conf)).unwrap();
    assert_eq!(config.device_match.as_deref(), Some("Jabra"));
    assert_eq!(config.language.as_deref(), Some("en"));
}

#[test]
fn config_build_unknown_flag_errors() {
    let dir = TempDir::new().unwrap();
    let conf = dir.path().join("meetmix.conf");
    std::fs::write(&conf, "--unknown=1\n").unwrap();
    let err = Config::build(&Overrides::default(), Some(&conf)).unwrap_err();
    assert!(format!("{}", err).contains("unsupported"));
}
