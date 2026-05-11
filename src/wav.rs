use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;

use anyhow::{Context, Result};

#[derive(Debug, Clone, PartialEq)]
pub struct WavStats {
    pub channels: u16,
    pub duration_seconds: f64,
    pub file_size: u64,
    pub peak: Option<i16>,
    pub rate: u32,
    pub rms: Option<f64>,
    pub sample_width_bytes: u16,
}

pub fn fix_wav_header(path: &Path) -> Result<bool> {
    let file_size = std::fs::metadata(path)
        .with_context(|| format!("reading metadata for {}", path.display()))?
        .len();
    if file_size < 44 {
        return Ok(false);
    }

    let mut file = OpenOptions::new().read(true).write(true).open(path)?;
    let mut header = [0_u8; 12];
    file.read_exact(&mut header)?;
    if &header[0..4] != b"RIFF" || &header[8..12] != b"WAVE" {
        return Ok(false);
    }

    let riff_size = u32::from_le_bytes(header[4..8].try_into().expect("riff size"));
    if riff_size as u64 == file_size - 8 {
        return Ok(false);
    }

    while file.stream_position()? < file_size.saturating_sub(8) {
        let mut chunk_id = [0_u8; 4];
        if file.read(&mut chunk_id)? < 4 {
            break;
        }
        let mut chunk_size_bytes = [0_u8; 4];
        if file.read(&mut chunk_size_bytes)? < 4 {
            break;
        }
        let chunk_size = u32::from_le_bytes(chunk_size_bytes);
        if &chunk_id == b"data" {
            let data_start = file.stream_position()?;
            let expected = file_size - data_start;
            if chunk_size as u64 != expected {
                file.seek(SeekFrom::Start(data_start - 4))?;
                file.write_all(&(expected as u32).to_le_bytes())?;
                file.seek(SeekFrom::Start(4))?;
                file.write_all(&((file_size - 8) as u32).to_le_bytes())?;
                return Ok(true);
            }
            return Ok(false);
        }
        file.seek(SeekFrom::Current(chunk_size as i64))?;
    }

    Ok(false)
}

pub fn analyze(path: &Path) -> Result<WavStats> {
    let file_size = std::fs::metadata(path)?.len();
    let mut file = OpenOptions::new().read(true).open(path)?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    if bytes.len() < 44 || &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        anyhow::bail!("not a WAV file");
    }

    let mut offset = 12;
    let mut channels = 0;
    let mut rate = 0;
    let mut sample_width_bytes = 0;
    let mut data = &[][..];

    while offset + 8 <= bytes.len() {
        let chunk_id = &bytes[offset..offset + 4];
        let chunk_size = u32::from_le_bytes(
            bytes[offset + 4..offset + 8]
                .try_into()
                .expect("chunk size"),
        ) as usize;
        offset += 8;
        if offset + chunk_size > bytes.len() {
            break;
        }
        if chunk_id == b"fmt " && chunk_size >= 16 {
            channels = u16::from_le_bytes(bytes[offset + 2..offset + 4].try_into().unwrap());
            rate = u32::from_le_bytes(bytes[offset + 4..offset + 8].try_into().unwrap());
            sample_width_bytes =
                u16::from_le_bytes(bytes[offset + 14..offset + 16].try_into().unwrap()) / 8;
        } else if chunk_id == b"data" {
            data = &bytes[offset..offset + chunk_size];
        }
        offset += chunk_size;
    }

    let frames = if channels > 0 && sample_width_bytes > 0 {
        data.len() as f64 / channels as f64 / sample_width_bytes as f64
    } else {
        0.0
    };
    let duration_seconds = if rate > 0 { frames / rate as f64 } else { 0.0 };
    let (peak, rms) = if sample_width_bytes == 2 {
        let mut peak = 0_i16;
        let mut sum = 0_f64;
        let mut count = 0_f64;
        for sample in data.chunks_exact(2) {
            let value = i16::from_le_bytes(sample.try_into().unwrap());
            peak = peak.max(value.saturating_abs());
            sum += f64::from(value) * f64::from(value);
            count += 1.0;
        }
        if count > 0.0 {
            (Some(peak), Some((sum / count).sqrt()))
        } else {
            (Some(0), Some(0.0))
        }
    } else {
        (None, None)
    };

    Ok(WavStats {
        channels,
        duration_seconds,
        file_size,
        peak,
        rate,
        rms,
        sample_width_bytes,
    })
}
