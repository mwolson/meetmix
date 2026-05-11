use std::io::{Read, Seek, SeekFrom};

use meetmix::wav::{analyze, fix_wav_header};
use tempfile::TempDir;

#[test]
fn fixes_truncated_wav_header() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("test.wav");
    std::fs::write(&path, build_wav(100, 1000, 1036)).unwrap();

    assert!(fix_wav_header(&path).unwrap());

    let mut file = std::fs::File::open(&path).unwrap();
    file.seek(SeekFrom::Start(4)).unwrap();
    let mut size = [0_u8; 4];
    file.read_exact(&mut size).unwrap();
    assert_eq!(
        u32::from_le_bytes(size) as u64,
        std::fs::metadata(&path).unwrap().len() - 8
    );

    file.seek(SeekFrom::Start(40)).unwrap();
    file.read_exact(&mut size).unwrap();
    assert_eq!(u32::from_le_bytes(size), 100);
}

#[test]
fn analyze_reports_basic_stats() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("test.wav");
    std::fs::write(&path, build_wav(4, 4, 40)).unwrap();
    let stats = analyze(&path).unwrap();
    assert_eq!(stats.channels, 1);
    assert_eq!(stats.rate, 16000);
    assert_eq!(stats.sample_width_bytes, 2);
    assert!(stats.peak.is_some());
}

fn build_wav(data_size: u32, data_size_in_header: u32, riff_size_in_header: u32) -> Vec<u8> {
    let mut bytes = Vec::new();
    bytes.extend_from_slice(b"RIFF");
    bytes.extend_from_slice(&riff_size_in_header.to_le_bytes());
    bytes.extend_from_slice(b"WAVE");
    bytes.extend_from_slice(b"fmt ");
    bytes.extend_from_slice(&16_u32.to_le_bytes());
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&1_u16.to_le_bytes());
    bytes.extend_from_slice(&16000_u32.to_le_bytes());
    bytes.extend_from_slice(&32000_u32.to_le_bytes());
    bytes.extend_from_slice(&2_u16.to_le_bytes());
    bytes.extend_from_slice(&16_u16.to_le_bytes());
    bytes.extend_from_slice(b"data");
    bytes.extend_from_slice(&data_size_in_header.to_le_bytes());
    bytes.extend(std::iter::repeat(0x40).take(data_size as usize));
    bytes
}
