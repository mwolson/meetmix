use clap::{Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(
    name = "meetmix",
    version,
    about = "Combine Bluetooth mic and speaker audio into one PipeWire recording for minutes"
)]
pub struct Cli {
    /// Substring to match Bluetooth device name or description
    #[arg(long = "device-match", global = true, value_name = "PATTERN")]
    pub device_match: Option<String>,

    /// Keep the WAV recording after successful processing
    #[arg(long, global = true)]
    pub keep_recording: bool,

    /// Transcription language code passed to minutes process
    #[arg(long, global = true, value_name = "CODE")]
    pub language: Option<String>,

    #[command(subcommand)]
    pub command: Option<Command>,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Remove orphaned meetmix PipeWire modules
    Cleanup,
    /// List audio sources and sinks
    Devices,
    /// Record with combined audio
    Record {
        /// Extra arguments passed to minutes process
        #[arg(
            value_name = "ARGS",
            trailing_var_arg = true,
            allow_hyphen_values = true
        )]
        extra_args: Vec<String>,
    },
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Overrides {
    pub device_match: Option<String>,
    pub language: Option<String>,
}

pub fn overrides(cli: &Cli) -> Overrides {
    Overrides {
        device_match: cli.device_match.clone(),
        language: cli.language.clone(),
    }
}
