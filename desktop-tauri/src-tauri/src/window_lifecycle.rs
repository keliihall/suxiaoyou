//! Cross-platform window visibility lifecycle.
//!
//! On macOS a native fullscreen window owns a separate Space. Hiding the
//! NSWindow before AppKit finishes leaving fullscreen strands the user on an
//! empty (black) Space. Keep the close-to-background intent pending until
//! AppKit posts its fullscreen-exit completion notification, then hide.

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::WebviewWindow;

struct BackgroundHideIntent(AtomicBool);

impl BackgroundHideIntent {
    const fn new() -> Self {
        Self(AtomicBool::new(false))
    }

    /// Return true only for the first request in the current transition.
    fn request(&self) -> bool {
        !self.0.swap(true, Ordering::AcqRel)
    }

    fn cancel(&self) {
        self.0.store(false, Ordering::Release);
    }

    fn is_pending(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }

    /// Atomically consume a pending request. A concurrent show wins.
    fn take(&self) -> bool {
        self.0
            .compare_exchange(true, false, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
    }
}

static BACKGROUND_HIDE: BackgroundHideIntent = BackgroundHideIntent::new();

/// Hide the main window while preserving the desktop's native fullscreen Space.
pub fn hide_to_background(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        // Tao updates its logical fullscreen state before AppKit's exit
        // animation completes. A second close during that animation must stay
        // a no-op instead of observing `false` and hiding too early.
        if BACKGROUND_HIDE.is_pending() {
            return Ok(());
        }
        let fullscreen = window.is_fullscreen().map_err(|error| error.to_string())?;
        if fullscreen {
            if !BACKGROUND_HIDE.request() {
                return Ok(());
            }
            if let Err(error) = window.set_fullscreen(false) {
                BACKGROUND_HIDE.cancel();
                return Err(error.to_string());
            }
            return Ok(());
        }
    }

    BACKGROUND_HIDE.cancel();
    window.hide().map_err(|error| error.to_string())
}

/// Register the authoritative AppKit fullscreen-exit completion callback.
#[cfg(target_os = "macos")]
pub fn install_fullscreen_exit_observer(window: &WebviewWindow) -> Result<(), String> {
    use std::{ptr::NonNull, sync::Once};

    use block2::RcBlock;
    use objc2::runtime::AnyObject;
    use objc2_app_kit::NSWindowDidExitFullScreenNotification;
    use objc2_foundation::{NSNotification, NSNotificationCenter};

    static INSTALL: Once = Once::new();
    let native_window = window.ns_window().map_err(|error| error.to_string())?;
    let callback_window = window.clone();

    INSTALL.call_once(|| {
        // SAFETY: Tauri returned this pointer for the live main NSWindow. The
        // observer is filtered to that exact object and AppKit posts the
        // notification on the window's main thread.
        let native_window = unsafe { &*native_window.cast::<AnyObject>() };
        let handler = RcBlock::new(move |_notification: NonNull<NSNotification>| {
            if BACKGROUND_HIDE.take() {
                if let Err(error) = callback_window.hide() {
                    log::warn!("Failed to hide window after leaving fullscreen: {error}");
                }
            }
        });
        let center = NSNotificationCenter::defaultCenter();
        // SAFETY: the notification name, NSWindow object, and block signature
        // match NSNotificationCenter's documented observer contract. The
        // center retains the observer for the lifetime of the application.
        unsafe {
            center.addObserverForName_object_queue_usingBlock(
                Some(NSWindowDidExitFullScreenNotification),
                Some(native_window),
                None,
                &handler,
            );
        }
    });
    Ok(())
}

/// Cancel any delayed hide before making a window visible again.
pub fn show_and_focus(window: &WebviewWindow) {
    BACKGROUND_HIDE.cancel();
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}

#[cfg(test)]
mod tests {
    use super::BackgroundHideIntent;

    #[test]
    fn repeated_close_requests_only_start_one_transition() {
        let intent = BackgroundHideIntent::new();
        assert!(intent.request());
        assert!(!intent.request());
        assert!(intent.is_pending());
        assert!(intent.take());
        assert!(!intent.take());
    }

    #[test]
    fn reopening_cancels_a_delayed_hide() {
        let intent = BackgroundHideIntent::new();
        assert!(intent.request());
        intent.cancel();
        assert!(!intent.is_pending());
        assert!(!intent.take());
    }
}
