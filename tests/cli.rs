use clap::Parser;
use meetmix::cli::{Cli, Command};

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
