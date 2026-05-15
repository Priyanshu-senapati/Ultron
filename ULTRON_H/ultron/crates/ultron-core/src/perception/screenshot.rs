//! # Screenshot Capture
//!
//! Captures the **primary monitor** via the classic GDI path
//! (`GetDC` → `CreateCompatibleDC`/`Bitmap` → `BitBlt` → `GetDIBits`),
//! converts the BGRA bottom-up bitmap GDI hands us into top-down RGBA,
//! and writes a PNG to `%APPDATA%\ULTRON\screenshots\` using the `image`
//! crate.
//!
//! GDI is preferred over Windows.Graphics.Capture for Phase 1 because:
//! - No WinRT activation context required, no app manifest, no UAC dance.
//! - Works the same in the Windows Service security context (no DWM
//!   compositor tricks needed for primary-monitor capture).
//! - Sub-30 ms on a 4K display on the target hardware.
//!
//! The **on-demand** path is just `Screenshotter::capture_now(reason)`.
//! The **periodic** path is `Screenshotter::start_periodic()`, which spawns
//! a Tokio task that calls `capture_now(Periodic)` on the configured
//! interval. Both publish [`UltronEvent::ScreenshotCaptured`] and append a
//! Quantum Log entry (path + dimensions only — never the pixels).
//!
//! On non-Windows targets the capture function returns an error so the
//! workspace still builds.

use crate::config::PerceptionConfig;
use crate::event_bus::EventBus;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tracing::{debug, info, warn};
use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{ScreenshotReason, UltronEvent};

#[derive(Clone)]
pub struct Screenshotter {
    cfg: PerceptionConfig,
    bus: EventBus,
    qlog: QuantumLog,
    output_dir: PathBuf,
    stop: Arc<AtomicBool>,
}

impl Screenshotter {
    pub fn new(cfg: PerceptionConfig, bus: EventBus, qlog: QuantumLog, data_dir: &Path) -> Self {
        let output_dir = data_dir.join("screenshots");
        Self {
            cfg,
            bus,
            qlog,
            output_dir,
            stop: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn output_dir(&self) -> &Path {
        &self.output_dir
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    /// Capture once, write to disk, publish the event, log it. Returns the
    /// produced path on success.
    pub async fn capture_now(&self, reason: ScreenshotReason) -> anyhow::Result<PathBuf> {
        std::fs::create_dir_all(&self.output_dir)?;
        let out = self.output_dir.clone();
        // GDI calls + PNG encoding must run on a blocking thread.
        let result = tokio::task::spawn_blocking(move || capture_to_dir(&out)).await??;

        let CaptureResult { path, width, height } = result;
        let ts = chrono::Utc::now().timestamp_millis();

        self.bus.publish(UltronEvent::ScreenshotCaptured {
            path: path.to_string_lossy().into_owned(),
            width,
            height,
            reason,
            ts_unix_ms: ts,
        });

        let _ = self
            .qlog
            .append_async(NewEntry::new(
                EntryKind::Event,
                "perception/screenshot",
                serde_json::json!({
                    "path": path.to_string_lossy(),
                    "width": width,
                    "height": height,
                    "reason": reason,
                    "ts_unix_ms": ts,
                }),
            ))
            .await;

        info!(
            path = %path.display(),
            w = width, h = height,
            ?reason,
            "screenshot captured"
        );
        Ok(path)
    }

    /// Spawn the periodic capture task. If `screenshot_interval_secs` is 0,
    /// no task is spawned — periodic mode is opt-in (it costs disk).
    pub fn start_periodic(&self) -> Option<tokio::task::JoinHandle<()>> {
        if self.cfg.screenshot_interval_secs == 0 {
            info!("periodic screenshots disabled (interval = 0)");
            return None;
        }
        let me = self.clone();
        let interval_secs = self.cfg.screenshot_interval_secs.max(5);
        Some(tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(interval_secs));
            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            // Skip the immediate first tick.
            interval.tick().await;
            info!(interval_secs, "periodic screenshotter started");
            while !me.stop.load(Ordering::SeqCst) {
                interval.tick().await;
                if me.stop.load(Ordering::SeqCst) {
                    break;
                }
                if let Err(e) = me.capture_now(ScreenshotReason::Periodic).await {
                    warn!("periodic screenshot failed: {e:?}");
                } else if let Err(e) = me.enforce_retention().await {
                    debug!("retention pass failed (non-fatal): {e:?}");
                }
            }
            info!("periodic screenshotter stopped");
        }))
    }

    /// Trim the screenshot directory to at most `screenshot_max_keep` files,
    /// oldest first. Best-effort; logs and moves on if anything fails.
    async fn enforce_retention(&self) -> anyhow::Result<()> {
        let dir = self.output_dir.clone();
        let max_keep = self.cfg.screenshot_max_keep;
        if max_keep == 0 {
            return Ok(());
        }
        tokio::task::spawn_blocking(move || -> anyhow::Result<()> {
            let mut entries: Vec<_> = std::fs::read_dir(&dir)?
                .filter_map(|e| e.ok())
                .filter(|e| {
                    e.path()
                        .extension()
                        .and_then(|x| x.to_str())
                        .map(|s| s.eq_ignore_ascii_case("png"))
                        .unwrap_or(false)
                })
                .collect();
            if entries.len() <= max_keep {
                return Ok(());
            }
            // Sort by modified time, oldest first.
            entries.sort_by_key(|e| {
                e.metadata()
                    .and_then(|m| m.modified())
                    .unwrap_or(std::time::UNIX_EPOCH)
            });
            let to_drop = entries.len() - max_keep;
            for e in entries.into_iter().take(to_drop) {
                let _ = std::fs::remove_file(e.path());
            }
            Ok(())
        })
        .await??;
        Ok(())
    }
}

struct CaptureResult {
    path: PathBuf,
    width: u32,
    height: u32,
}

fn timestamp_filename() -> String {
    // Local-time stamp matches what the user sees in their file explorer.
    let now = chrono::Local::now();
    format!("screenshot_{}.png", now.format("%Y%m%d_%H%M%S_%3f"))
}

// =====================================================================
// Windows GDI capture.
// =====================================================================

#[cfg(windows)]
fn capture_to_dir(dir: &Path) -> anyhow::Result<CaptureResult> {
    use std::ffi::c_void;
    use windows::Win32::Foundation::HWND;
    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, DIB_RGB_COLORS, HDC,
        SRCCOPY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};

    unsafe {
        let width = GetSystemMetrics(SM_CXSCREEN);
        let height = GetSystemMetrics(SM_CYSCREEN);
        if width <= 0 || height <= 0 {
            anyhow::bail!("invalid screen dimensions: {width}x{height}");
        }

        // Source DC = the whole screen.
        let screen_dc: HDC = GetDC(HWND(std::ptr::null_mut()));
        if screen_dc.0.is_null() {
            anyhow::bail!("GetDC(NULL) failed");
        }
        // Memory DC compatible with the screen.
        let mem_dc: HDC = CreateCompatibleDC(screen_dc);
        if mem_dc.0.is_null() {
            ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
            anyhow::bail!("CreateCompatibleDC failed");
        }
        let bitmap = CreateCompatibleBitmap(screen_dc, width, height);
        if bitmap.0.is_null() {
            let _ = DeleteDC(mem_dc);
            ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
            anyhow::bail!("CreateCompatibleBitmap failed");
        }
        let old_obj = SelectObject(mem_dc, bitmap);

        // Blit the screen into the memory bitmap.
        let blt = BitBlt(mem_dc, 0, 0, width, height, screen_dc, 0, 0, SRCCOPY);
        if blt.is_err() {
            SelectObject(mem_dc, old_obj);
            let _ = DeleteObject(bitmap);
            let _ = DeleteDC(mem_dc);
            ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
            anyhow::bail!("BitBlt failed");
        }

        // Pull pixels out as 32-bit BGRA. GDI gives us bottom-up rows
        // when biHeight is positive — using a *negative* biHeight asks for
        // top-down rows directly, saving us a flip.
        let mut bmi: BITMAPINFO = std::mem::zeroed();
        bmi.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: width,
            biHeight: -height, // negative = top-down
            biPlanes: 1,
            biBitCount: 32,
            biCompression: 0, // BI_RGB — value is 0 per the Win32 API; we hard-code rather than import to avoid windows-rs version sensitivity (BI_RGB has been both a u32 const and a BI_COMPRESSION newtype across releases).
            biSizeImage: 0,
            biXPelsPerMeter: 0,
            biYPelsPerMeter: 0,
            biClrUsed: 0,
            biClrImportant: 0,
        };
        // bmiColors is already zeroed by `std::mem::zeroed()` above; we don't
        // need it for 32-bit BI_RGB anyway (no palette).

        let stride = (width as usize) * 4;
        let mut buf = vec![0u8; stride * (height as usize)];

        let lines = GetDIBits(
            mem_dc,
            bitmap,
            0,
            height as u32,
            Some(buf.as_mut_ptr() as *mut c_void),
            &mut bmi,
            DIB_RGB_COLORS,
        );

        // Cleanup.
        SelectObject(mem_dc, old_obj);
        let _ = DeleteObject(bitmap);
        let _ = DeleteDC(mem_dc);
        ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);

        if lines == 0 {
            anyhow::bail!("GetDIBits returned 0 lines");
        }

        // GDI gives BGRA. Image crate wants RGBA. Swap in place.
        for px in buf.chunks_exact_mut(4) {
            px.swap(0, 2); // B <-> R
        }

        // Encode and write.
        let img = image::RgbaImage::from_raw(width as u32, height as u32, buf)
            .ok_or_else(|| anyhow::anyhow!("failed to build RgbaImage from raw buffer"))?;

        std::fs::create_dir_all(dir)?;
        let path = dir.join(timestamp_filename());
        img.save(&path)?;

        Ok(CaptureResult {
            path,
            width: width as u32,
            height: height as u32,
        })
    }
}

#[cfg(not(windows))]
fn capture_to_dir(_dir: &Path) -> anyhow::Result<CaptureResult> {
    anyhow::bail!("screenshot capture not supported on this platform")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_qlog() -> QuantumLog {
        let mut p = std::env::temp_dir();
        p.push(format!("ultron_ss_test_{}.db", uuid::Uuid::new_v4()));
        QuantumLog::open(&p).unwrap()
    }

    #[test]
    fn periodic_disabled_returns_none() {
        let bus = EventBus::new(8);
        let mut cfg = PerceptionConfig::default();
        cfg.screenshot_interval_secs = 0;
        let dir = std::env::temp_dir();
        let s = Screenshotter::new(cfg, bus, temp_qlog(), &dir);
        assert!(s.start_periodic().is_none());
    }

    #[test]
    fn timestamp_filename_format() {
        let n = timestamp_filename();
        assert!(n.starts_with("screenshot_"));
        assert!(n.ends_with(".png"));
    }

    #[cfg(not(windows))]
    #[test]
    fn capture_errors_on_non_windows() {
        let r = capture_to_dir(&std::env::temp_dir());
        assert!(r.is_err());
    }
}
