//! # Global Input Monitor
//!
//! Installs Windows low-level hooks (`WH_KEYBOARD_LL`, `WH_MOUSE_LL`) on a
//! dedicated thread and forwards **privacy-respecting** metadata (categories,
//! timing, deltas) onto the event bus.
//!
//! ## What we deliberately do **not** capture
//! - Actual keystroke characters (we classify into [`KeyCategory`] only)
//! - Window titles or process names (that's the screen engine's job, Phase 1)
//! - Clipboard contents
//!
//! On non-Windows targets this module compiles to a no-op stub so the
//! workspace still builds for CI on Linux / macOS.

use crate::config::InputConfig;
use crate::event_bus::EventBus;
use crate::perception::InputMetricsAggregator;
use crate::tension::TensionTracker;
use std::sync::Arc;

#[cfg(windows)]
use crossbeam_channel::{bounded, Receiver, Sender};
#[cfg(windows)]
use parking_lot::Mutex;
#[cfg(windows)]
use std::sync::atomic::{AtomicBool, Ordering};

use ultron_quantum_log::{EntryKind, NewEntry, QuantumLog};
use ultron_types::{InputSignal, UltronEvent};

#[cfg(windows)]
use ultron_types::{modifier_bits, KeyCategory, MouseButton};

#[derive(Clone)]
pub struct InputMonitor {
    cfg: InputConfig,
    bus: EventBus,
    tracker: TensionTracker,
    metrics: InputMetricsAggregator,
    qlog: QuantumLog,
    #[cfg(windows)]
    stop: Arc<AtomicBool>,
    #[cfg(windows)]
    hook_thread: Arc<Mutex<Option<std::thread::JoinHandle<()>>>>,
    #[cfg(windows)]
    hook_thread_id: Arc<Mutex<Option<u32>>>,
}

impl InputMonitor {
    pub fn new(
        cfg: InputConfig,
        bus: EventBus,
        tracker: TensionTracker,
        metrics: InputMetricsAggregator,
        qlog: QuantumLog,
    ) -> Self {
        Self {
            cfg,
            bus,
            tracker,
            metrics,
            qlog,
            #[cfg(windows)]
            stop: Arc::new(AtomicBool::new(false)),
            #[cfg(windows)]
            hook_thread: Arc::new(Mutex::new(None)),
            #[cfg(windows)]
            hook_thread_id: Arc::new(Mutex::new(None)),
        }
    }

    /// Start the hook thread and the async forwarder task.
    pub fn start(&self) -> anyhow::Result<()> {
        #[cfg(windows)]
        {
            self.start_windows()
        }
        #[cfg(not(windows))]
        {
            tracing::warn!("input monitor: not on Windows, running in stub mode");
            Ok(())
        }
    }

    /// Stop hooks and join the thread. Idempotent.
    pub fn stop(&self) {
        #[cfg(windows)]
        {
            self.stop_windows();
        }
    }
}

// ============================================================================
// Windows implementation
// ============================================================================

#[cfg(windows)]
mod win_impl {
    use super::*;
    use std::sync::OnceLock;
    use std::time::{Duration, Instant};
    use tracing::{debug, error, info, warn};
    use windows::Win32::Foundation::{HINSTANCE, LPARAM, LRESULT, WPARAM};
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::System::Threading::GetCurrentThreadId;
    use windows::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VIRTUAL_KEY};
    use windows::Win32::UI::WindowsAndMessaging::{
        CallNextHookEx, DispatchMessageW, GetMessageW, PostThreadMessageW, SetWindowsHookExW,
        TranslateMessage, UnhookWindowsHookEx, HHOOK, KBDLLHOOKSTRUCT, MSG, MSLLHOOKSTRUCT,
        WH_KEYBOARD_LL, WH_MOUSE_LL, WM_KEYDOWN, WM_KEYUP, WM_LBUTTONDOWN, WM_LBUTTONUP,
        WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_QUIT, WM_RBUTTONDOWN,
        WM_RBUTTONUP, WM_SYSKEYDOWN, WM_SYSKEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP,
    };

    /// Sender used by the hook callbacks. Must be a process-global static
    /// because `HOOKPROC` is a plain `extern "system" fn`.
    static SENDER: OnceLock<Sender<InputSignal>> = OnceLock::new();

    /// Last mouse position, for delta computation in the move callback.
    static LAST_POS: parking_lot::Mutex<Option<(i32, i32)>> = parking_lot::Mutex::new(None);
    /// Last move emit time (ms since boot of monotonic clock) — throttle.
    static LAST_MOVE_MS: parking_lot::Mutex<Option<Instant>> = parking_lot::Mutex::new(None);
    /// Configurable throttle, lifted into a static so the callback can read it.
    static MOUSE_MOVE_MIN_INTERVAL_MS: std::sync::atomic::AtomicU64 =
        std::sync::atomic::AtomicU64::new(50);

    impl super::InputMonitor {
        pub(super) fn start_windows(&self) -> anyhow::Result<()> {
            let (tx, rx): (Sender<InputSignal>, Receiver<InputSignal>) = bounded(4096);
            SENDER
                .set(tx)
                .map_err(|_| anyhow::anyhow!("input monitor already started"))?;
            MOUSE_MOVE_MIN_INTERVAL_MS
                .store(self.cfg.mouse_move_min_interval_ms, Ordering::Relaxed);

            let kb = self.cfg.enable_keyboard_hook;
            let ms = self.cfg.enable_mouse_hook;
            let stop_flag = self.stop.clone();
            let thread_id_slot = self.hook_thread_id.clone();

            let handle = std::thread::Builder::new()
                .name("ultron-hooks".into())
                .spawn(move || {
                    // Capture this thread's id so the main runtime can post WM_QUIT.
                    *thread_id_slot.lock() = Some(unsafe { GetCurrentThreadId() });

                    let hmod: HINSTANCE = match unsafe { GetModuleHandleW(None) } {
                        Ok(h) => h.into(),
                        Err(e) => {
                            error!("GetModuleHandleW failed: {e:?}");
                            return;
                        }
                    };

                    let mut kb_hook: Option<HHOOK> = None;
                    let mut ms_hook: Option<HHOOK> = None;
                    if kb {
                        match unsafe {
                            SetWindowsHookExW(WH_KEYBOARD_LL, Some(keyboard_proc), hmod, 0)
                        } {
                            Ok(h) => {
                                kb_hook = Some(h);
                                info!("keyboard low-level hook installed");
                            }
                            Err(e) => error!("keyboard hook install failed: {e:?}"),
                        }
                    }
                    if ms {
                        match unsafe {
                            SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_proc), hmod, 0)
                        } {
                            Ok(h) => {
                                ms_hook = Some(h);
                                info!("mouse low-level hook installed");
                            }
                            Err(e) => error!("mouse hook install failed: {e:?}"),
                        }
                    }

                    // Standard Windows message loop. Hooks fire synchronously on this
                    // thread between GetMessage / Translate / Dispatch calls.
                    let mut msg = MSG::default();
                    loop {
                        if stop_flag.load(Ordering::SeqCst) {
                            break;
                        }
                        let r = unsafe { GetMessageW(&mut msg, None, 0, 0) };
                        if r.0 <= 0 {
                            // 0 = WM_QUIT, -1 = error
                            break;
                        }
                        unsafe {
                            let _ = TranslateMessage(&msg);
                            DispatchMessageW(&msg);
                        }
                    }

                    if let Some(h) = kb_hook {
                        unsafe {
                            let _ = UnhookWindowsHookEx(h);
                        }
                    }
                    if let Some(h) = ms_hook {
                        unsafe {
                            let _ = UnhookWindowsHookEx(h);
                        }
                    }
                    info!("input hook thread exiting");
                })?;
            *self.hook_thread.lock() = Some(handle);

            // Async forwarder: rx -> tracker.feed + metrics.feed_input + bus.publish + qlog (sampled).
            let bus = self.bus.clone();
            let tracker = self.tracker.clone();
            let metrics = self.metrics.clone();
            let qlog = self.qlog.clone();
            tokio::spawn(async move {
                let mut sample_counter: u64 = 0;
                loop {
                    // crossbeam recv is sync; do it on a blocking thread.
                    let next = tokio::task::spawn_blocking({
                        let rx = rx.clone();
                        move || rx.recv().ok()
                    })
                    .await
                    .ok()
                    .flatten();
                    let Some(sig) = next else { break };

                    tracker.feed(&sig);
                    metrics.feed_input(&sig);
                    bus.publish(UltronEvent::InputActivity(sig.clone()));

                    // Quantum-log a small sample (1 in 64) to avoid runaway DB
                    // size. Backspaces and clicks are always logged because
                    // they're meaningful for downstream modules.
                    sample_counter = sample_counter.wrapping_add(1);
                    let always_log = matches!(
                        &sig,
                        InputSignal::KeyEvent { category: KeyCategory::Backspace, .. }
                        | InputSignal::MouseButton { .. }
                    );
                    if always_log || sample_counter % 64 == 0 {
                        let payload = serde_json::to_value(&sig).unwrap_or(serde_json::json!({}));
                        if let Err(e) = qlog
                            .append_async(NewEntry::new(EntryKind::Input, "input_monitor", payload))
                            .await
                        {
                            warn!("qlog append failed in input_monitor: {e}");
                        }
                    }
                }
                debug!("input forwarder loop ended");
            });

            Ok(())
        }

        pub(super) fn stop_windows(&self) {
            self.stop.store(true, Ordering::SeqCst);
            // Wake the hook thread out of GetMessageW.
            if let Some(tid) = *self.hook_thread_id.lock() {
                unsafe {
                    let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
                }
            }
            if let Some(h) = self.hook_thread.lock().take() {
                let _ = h.join();
            }
        }
    }

    // ---------------------- HOOK CALLBACKS ----------------------

    unsafe extern "system" fn keyboard_proc(
        code: i32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        if code >= 0 {
            let info = &*(lparam.0 as *const KBDLLHOOKSTRUCT);
            let vk = info.vkCode;
            let is_down = matches!(wparam.0 as u32, WM_KEYDOWN | WM_SYSKEYDOWN);
            let category = classify_vk(vk);
            let modifier_mask = current_modifier_mask();
            if let Some(s) = SENDER.get() {
                let _ = s.try_send(InputSignal::KeyEvent {
                    ts_ms: chrono::Utc::now().timestamp_millis(),
                    category,
                    modifier_mask,
                    is_down,
                });
            }
        }
        CallNextHookEx(HHOOK::default(), code, wparam, lparam)
    }

    unsafe extern "system" fn mouse_proc(
        code: i32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        if code >= 0 {
            let info = &*(lparam.0 as *const MSLLHOOKSTRUCT);
            let now_ms = chrono::Utc::now().timestamp_millis();
            let pt = info.pt;
            match wparam.0 as u32 {
                WM_MOUSEMOVE => {
                    // Throttle move events.
                    let min_iv = MOUSE_MOVE_MIN_INTERVAL_MS.load(Ordering::Relaxed);
                    let now = Instant::now();
                    let mut t = LAST_MOVE_MS.lock();
                    let do_emit = match *t {
                        None => true,
                        Some(prev) => now.duration_since(prev) >= Duration::from_millis(min_iv),
                    };
                    if do_emit {
                        *t = Some(now);
                        drop(t);

                        let mut last = LAST_POS.lock();
                        let (dx, dy) = match *last {
                            Some((px, py)) => (pt.x - px, pt.y - py),
                            None => (0, 0),
                        };
                        *last = Some((pt.x, pt.y));
                        if let Some(s) = SENDER.get() {
                            let _ = s.try_send(InputSignal::MouseMove {
                                ts_ms: now_ms,
                                dx,
                                dy,
                            });
                        }
                    }
                }
                WM_LBUTTONDOWN | WM_LBUTTONUP => emit_button(
                    MouseButton::Left,
                    matches!(wparam.0 as u32, WM_LBUTTONDOWN),
                    now_ms,
                ),
                WM_RBUTTONDOWN | WM_RBUTTONUP => emit_button(
                    MouseButton::Right,
                    matches!(wparam.0 as u32, WM_RBUTTONDOWN),
                    now_ms,
                ),
                WM_MBUTTONDOWN | WM_MBUTTONUP => emit_button(
                    MouseButton::Middle,
                    matches!(wparam.0 as u32, WM_MBUTTONDOWN),
                    now_ms,
                ),
                WM_XBUTTONDOWN | WM_XBUTTONUP => {
                    // High word of mouseData identifies XBUTTON1 vs XBUTTON2.
                    let xbutton = (info.mouseData >> 16) & 0xFFFF;
                    let btn = if xbutton == 1 {
                        MouseButton::X1
                    } else {
                        MouseButton::X2
                    };
                    emit_button(btn, matches!(wparam.0 as u32, WM_XBUTTONDOWN), now_ms);
                }
                WM_MOUSEWHEEL => {
                    let high = ((info.mouseData >> 16) & 0xFFFF) as i16;
                    if let Some(s) = SENDER.get() {
                        let _ = s.try_send(InputSignal::MouseScroll {
                            ts_ms: now_ms,
                            delta: high as i32,
                        });
                    }
                }
                _ => {}
            }
        }
        CallNextHookEx(HHOOK::default(), code, wparam, lparam)
    }

    fn emit_button(button: MouseButton, is_down: bool, ts_ms: i64) {
        if let Some(s) = SENDER.get() {
            let _ = s.try_send(InputSignal::MouseButton {
                ts_ms,
                button,
                is_down,
            });
        }
    }

    fn classify_vk(vk: u32) -> KeyCategory {
        match vk {
            // Backspace gets its own category — primary error proxy.
            0x08 => KeyCategory::Backspace,
            // Tab / Enter / Space
            0x09 | 0x0D | 0x20 => KeyCategory::Whitespace,
            // Shift / Ctrl / Alt / Win
            0x10..=0x12 | 0xA0..=0xA5 | 0x5B..=0x5C => KeyCategory::Modifier,
            // 0-9
            0x30..=0x39 => KeyCategory::Digit,
            // A-Z
            0x41..=0x5A => KeyCategory::Letter,
            // PgUp / PgDn / End / Home / Arrows / Ins / Del
            0x21..=0x28 | 0x2D | 0x2E => KeyCategory::Navigation,
            // F1..F24
            0x70..=0x87 => KeyCategory::Function,
            // Esc / CapsLock / NumLock / ScrollLock / Pause / PrintScreen
            0x1B | 0x14 | 0x90 | 0x91 | 0x13 | 0x2C => KeyCategory::System,
            // Common OEM punctuation ranges
            0xBA..=0xC0 | 0xDB..=0xDF | 0xE2 => KeyCategory::Symbol,
            _ => KeyCategory::Unknown,
        }
    }

    fn current_modifier_mask() -> u8 {
        let mut m = 0u8;
        unsafe {
            // High bit (0x8000) = key is down.
            if (GetAsyncKeyState(VIRTUAL_KEY(0x10).0 as i32) as u16) & 0x8000 != 0 {
                m |= modifier_bits::SHIFT;
            }
            if (GetAsyncKeyState(VIRTUAL_KEY(0x11).0 as i32) as u16) & 0x8000 != 0 {
                m |= modifier_bits::CTRL;
            }
            if (GetAsyncKeyState(VIRTUAL_KEY(0x12).0 as i32) as u16) & 0x8000 != 0 {
                m |= modifier_bits::ALT;
            }
            // LWin (0x5B) or RWin (0x5C)
            if ((GetAsyncKeyState(VIRTUAL_KEY(0x5B).0 as i32) as u16) & 0x8000 != 0)
                || ((GetAsyncKeyState(VIRTUAL_KEY(0x5C).0 as i32) as u16) & 0x8000 != 0)
            {
                m |= modifier_bits::WIN;
            }
        }
        m
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{PerceptionConfig, TensionConfig};

    #[test]
    fn stub_or_real_compiles() {
        let bus = EventBus::new(8);
        let tracker = TensionTracker::new(
            TensionConfig {
                ewma_alpha: 0.1,
                decay_per_sec: 0.01,
                w_typing_volatility: 0.4,
                w_click_rate: 0.2,
                w_error_signal: 0.4,
                w_idle: 0.1,
            },
            bus.clone(),
        );
        // Use a temp DB so this test runs anywhere.
        let mut p = std::env::temp_dir();
        p.push(format!("ultron_im_test_{}.db", std::process::id()));
        let _ = std::fs::remove_file(&p);
        let qlog = ultron_quantum_log::QuantumLog::open(&p).unwrap();

        let metrics = InputMetricsAggregator::new(
            PerceptionConfig::default(),
            bus.clone(),
            qlog.clone(),
        );

        let im = InputMonitor::new(
            InputConfig {
                enable_keyboard_hook: false,
                enable_mouse_hook: false,
                mouse_move_min_interval_ms: 50,
                idle_threshold_secs: 90,
            },
            bus,
            tracker,
            metrics,
            qlog,
        );
        // Don't actually start hooks in unit tests — would require a Windows
        // message loop and admin-equivalent privileges in some setups. Just
        // verify the type constructs and stops cleanly.
        im.stop();
        let _ = std::fs::remove_file(&p);
    }
}
