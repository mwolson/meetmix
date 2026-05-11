use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use regex::Regex;

use crate::cli::Overrides;

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Config {
    pub device_match: Option<String>,
    pub language: Option<String>,
}

impl Config {
    pub fn build(overrides: &Overrides, conf_path: Option<&Path>) -> Result<Self> {
        let mut config = Config::default();
        if let Some(path) = conf_path {
            if path.exists() {
                for (flag, value) in parse_conf(&std::fs::read_to_string(path)?, path)? {
                    match flag.as_str() {
                        "--device-match" => config.device_match = Some(value),
                        "--language" => config.language = Some(value),
                        _ => bail!("unsupported flag '{}' in {}", flag, path.display()),
                    }
                }
            }
        }
        if overrides.device_match.is_some() {
            config.device_match = overrides.device_match.clone();
        }
        if overrides.language.is_some() {
            config.language = overrides.language.clone();
        }
        Ok(config)
    }
}

pub fn default_conf_path() -> Option<PathBuf> {
    if let Some(home) = std::env::var_os("XDG_CONFIG_HOME") {
        return Some(PathBuf::from(home).join("meetmix.conf"));
    }
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config/meetmix.conf"))
}

pub fn parse_conf(text: &str, path: &Path) -> Result<Vec<(String, String)>> {
    let line_re = Regex::new(r"^(--[a-z][a-z0-9-]*)=(.+)$").expect("config line regex");
    let mut entries = Vec::new();
    for (idx, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let caps = line_re.captures(line).with_context(|| {
            format!("malformed line {} in {}: {}", idx + 1, path.display(), line)
        })?;
        entries.push((caps[1].to_string(), caps[2].to_string()));
    }
    Ok(entries)
}
