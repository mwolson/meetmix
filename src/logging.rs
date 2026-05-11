use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use chrono::Local;

pub struct Logger {
    file: Option<File>,
}

impl Logger {
    pub fn new(path: &Path) -> Result<Self> {
        let parent = path.parent().context("log path has no parent")?;
        std::fs::create_dir_all(parent)?;
        let file = OpenOptions::new()
            .append(true)
            .create(true)
            .open(path)
            .with_context(|| format!("opening {}", path.display()))?;
        let mut logger = Self { file: Some(file) };
        logger.log(format!("Log file: {}", path.display()))?;
        Ok(logger)
    }

    pub fn null() -> Self {
        Self { file: None }
    }

    pub fn log(&mut self, message: impl AsRef<str>) -> io::Result<()> {
        let message = message.as_ref();
        println!("{}", message);
        self.write_line(message)
    }

    pub fn warn(&mut self, message: impl AsRef<str>) -> io::Result<()> {
        let message = message.as_ref();
        eprintln!("{}", message);
        self.write_line(&format!("WARNING: {}", message))
    }

    pub fn log_file_only(&mut self, message: impl AsRef<str>) -> io::Result<()> {
        self.write_line(message.as_ref())
    }

    pub fn clone_file(&self) -> io::Result<Option<File>> {
        self.file.as_ref().map(File::try_clone).transpose()
    }

    fn write_line(&mut self, message: &str) -> io::Result<()> {
        if let Some(file) = &mut self.file {
            let ts = Local::now().format("%H:%M:%S");
            writeln!(file, "[{}] {}", ts, message)?;
            file.flush()?;
        }
        Ok(())
    }
}

pub fn log_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".minutes/logs"))
}

pub fn recording_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join("meetings/recordings"))
}
