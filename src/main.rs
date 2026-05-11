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
            let live_transcript = !parsed.no_live;
            let record_backend = if parsed.no_live {
                cli::RecordBackend::PwRecord
            } else {
                parsed.record_backend
            };
            let mut required = vec![
                "minutes",
                "pactl",
                "pw-dump",
                "pw-link",
                "pw-loopback",
                "pw-play",
                "wpctl",
            ];
            if record_backend == cli::RecordBackend::PwRecord {
                required.push("pw-record");
            }
            deps::check_required(&required)?;
            pipeline::run_record(
                config,
                parsed.keep_recording,
                record_backend,
                live_transcript,
                extra_args,
            )
        }
    }
}
