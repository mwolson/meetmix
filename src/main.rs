use anyhow::Result;
use clap::Parser;
use meetmix::{cli, config, deps, pipeline};

fn main() {
    let parsed = cli::Cli::parse();
    match dispatch(parsed) {
        Ok(code) => std::process::exit(code),
        Err(err) => {
            eprintln!("Error: {:#}", err);
            std::process::exit(1);
        }
    }
}

fn dispatch(parsed: cli::Cli) -> Result<i32> {
    let overrides = cli::overrides(&parsed);
    let config = config::Config::build(&overrides, config::default_conf_path().as_deref())?;
    match parsed.command.unwrap_or(cli::Command::Record {
        extra_args: Vec::new(),
    }) {
        cli::Command::Cleanup => {
            deps::check_required(&["pactl"])?;
            pipeline::run_cleanup()?;
            Ok(0)
        }
        cli::Command::Devices => {
            deps::check_required(&["pactl"])?;
            pipeline::run_devices(&config)?;
            Ok(0)
        }
        cli::Command::Record { extra_args } => {
            deps::check_required(&[
                "minutes",
                "pactl",
                "pw-dump",
                "pw-link",
                "pw-loopback",
                "pw-play",
                "pw-record",
                "wpctl",
            ])?;
            pipeline::run_record(config, parsed.keep_recording, extra_args)
        }
    }
}
