use clap::Parser;
use meetmix::cli::{Cli, Command, RecordBackend};

#[test]
fn no_subcommand_defaults_to_record_dispatch() {
    let cli = Cli::try_parse_from(["meetmix"]).unwrap();
    assert!(cli.command.is_none());
}

#[test]
fn record_accepts_global_flags_after_subcommand() {
    let cli = Cli::try_parse_from(["meetmix", "record", "--device-match", "AirPods"]).unwrap();
    assert_eq!(cli.device_match.as_deref(), Some("AirPods"));
}

#[test]
fn record_passes_unknown_flags_through() {
    let cli = Cli::try_parse_from(["meetmix", "record", "--unknown-flag", "foo"]).unwrap();
    let Some(Command::Record { extra_args }) = cli.command else {
        panic!("expected record command");
    };
    assert_eq!(extra_args, vec!["--unknown-flag", "foo"]);
}

#[test]
fn record_backend_defaults_to_pw_record() {
    let cli = Cli::try_parse_from(["meetmix"]).unwrap();
    assert_eq!(cli.record_backend, RecordBackend::PwRecord);
}

#[test]
fn record_backend_accepts_minutes() {
    let cli = Cli::try_parse_from(["meetmix", "--record-backend", "minutes"]).unwrap();
    assert_eq!(cli.record_backend, RecordBackend::Minutes);
}

#[test]
fn no_live_flag_is_accepted() {
    let cli = Cli::try_parse_from(["meetmix", "--no-live"]).unwrap();
    assert!(cli.no_live);
}
