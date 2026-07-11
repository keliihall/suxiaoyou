//! Tauri command handlers — the IPC bridge between frontend and Rust.

use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

use tauri::{AppHandle, Manager, WebviewWindow};
use tauri_plugin_opener::OpenerExt;

use crate::{
    backend::{BackendState, BackendStatus},
    tray, PendingNavigationState,
};

/// Get the backend URL (http://127.0.0.1:{port}).
#[tauri::command]
pub async fn get_backend_url(state: tauri::State<'_, BackendState>) -> Result<String, String> {
    Ok(state.url().await)
}

/// Get the backend's per-run session bearer token. The token is read
/// from a 0600 file the backend writes on startup, so another local
/// user on the same host cannot obtain it. The frontend attaches it
/// as `Authorization: Bearer ...` on every API request and as a
/// `?token=` query param on EventSource streams (which cannot set
/// custom headers). Never log this value.
#[tauri::command]
pub async fn get_backend_token(state: tauri::State<'_, BackendState>) -> Result<String, String> {
    state.token().await
}

/// Get the latest revisioned native backend lifecycle snapshot.
#[tauri::command]
pub async fn get_backend_status(
    state: tauri::State<'_, BackendState>,
) -> Result<BackendStatus, String> {
    Ok(state.status().await)
}

#[tauri::command]
pub async fn get_pending_navigation(
    state: tauri::State<'_, PendingNavigationState>,
) -> Result<Option<String>, String> {
    Ok(state.take().await)
}

/// Minimize the window.
#[tauri::command]
pub fn window_minimize(window: WebviewWindow) -> Result<(), String> {
    window.minimize().map_err(|e| e.to_string())
}

/// Toggle maximize/unmaximize.
#[tauri::command]
pub fn window_maximize(window: WebviewWindow) -> Result<(), String> {
    if window.is_maximized().unwrap_or(false) {
        window.unmaximize().map_err(|e| e.to_string())
    } else {
        window.maximize().map_err(|e| e.to_string())
    }
}

/// Close the window (hides to tray/dock on all platforms).
#[tauri::command]
pub fn window_close(window: WebviewWindow) -> Result<(), String> {
    window.hide().map_err(|e| e.to_string())
}

/// Check if window is maximized.
#[tauri::command]
pub fn is_maximized(window: WebviewWindow) -> Result<bool, String> {
    window.is_maximized().map_err(|e| e.to_string())
}

/// Get the current platform.
#[tauri::command]
pub fn get_platform() -> String {
    std::env::consts::OS.to_string()
}

/// Open a URL in the system default browser.
#[tauri::command]
pub fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| e.to_string())
}

/// Open the application's fixed log directory in the system file manager.
/// No caller-supplied path is accepted across IPC.
#[tauri::command]
pub fn open_backend_logs(app: AppHandle) -> Result<(), String> {
    let log_dir = app
        .path()
        .app_log_dir()
        .map_err(|e| format!("Failed to get app log directory: {e}"))?;
    std::fs::create_dir_all(&log_dir)
        .map_err(|e| format!("Failed to create app log directory: {e}"))?;

    app.opener()
        .open_path(log_dir.to_string_lossy().into_owned(), None::<String>)
        .map_err(|e| format!("Failed to open app log directory: {e}"))
}

/// Stop and reap the backend before restarting the desktop process.
///
/// The generic process-plugin restart exits immediately and can bypass the
/// asynchronous ExitRequested cleanup, so recovery UI must use this command.
#[tauri::command]
pub async fn relaunch_app(
    app: AppHandle,
    state: tauri::State<'_, BackendState>,
) -> Result<(), String> {
    state.stop().await?;
    app.restart()
}

static SAVE_TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct TemporaryFileGuard {
    path: Option<PathBuf>,
}

impl TemporaryFileGuard {
    fn new(path: PathBuf) -> Self {
        Self { path: Some(path) }
    }

    fn disarm(&mut self) {
        self.path = None;
    }

    fn cleanup(&mut self) -> io::Result<()> {
        let Some(path) = self.path.as_ref() else {
            return Ok(());
        };

        match fs::remove_file(path) {
            Ok(()) => {
                self.path = None;
                Ok(())
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                self.path = None;
                Ok(())
            }
            Err(error) => Err(error),
        }
    }
}

impl Drop for TemporaryFileGuard {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

fn validate_save_target(target: &Path) -> io::Result<()> {
    if !target.is_absolute() || target.file_name().is_none() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "save target must be an absolute file path",
        ));
    }

    let parent = target.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "save target must have a parent directory",
        )
    })?;
    let parent_metadata = fs::metadata(parent)?;
    if !parent_metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "save target parent must be a directory",
        ));
    }

    match fs::symlink_metadata(target) {
        Ok(metadata) if metadata.file_type().is_file() => Ok(()),
        Ok(_) => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "save target must be a regular file",
        )),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn create_save_temporary_file(parent: &Path) -> io::Result<(PathBuf, File)> {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();

    for _ in 0..128 {
        let sequence = SAVE_TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let candidate = parent.join(format!(
            ".suxiaoyou-save-{}-{timestamp:x}-{sequence:x}.tmp",
            std::process::id()
        ));

        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&candidate)
        {
            Ok(file) => return Ok((candidate, file)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }

    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "could not allocate a unique save temporary file",
    ))
}

#[cfg(unix)]
fn install_save_temporary_file(temporary: &Path, target: &Path) -> io::Result<()> {
    validate_save_target(target)?;
    fs::rename(temporary, target)
}

#[cfg(windows)]
fn install_save_temporary_file(temporary: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    fn to_wide(path: &Path) -> io::Result<Vec<u16>> {
        let mut encoded: Vec<u16> = path.as_os_str().encode_wide().collect();
        if encoded.contains(&0) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "save path contains a NUL character",
            ));
        }
        encoded.push(0);
        Ok(encoded)
    }

    validate_save_target(target)?;
    let temporary_wide = to_wide(temporary)?;
    let target_wide = to_wide(target)?;

    // Both paths are in the same directory, so this is a single rename with
    // replacement rather than a copy/delete fallback. In particular, there is
    // never a window where we delete an existing destination before install.
    let installed = unsafe {
        MoveFileExW(
            temporary_wide.as_ptr(),
            target_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };

    if installed == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(any(unix, windows)))]
fn install_save_temporary_file(temporary: &Path, target: &Path) -> io::Result<()> {
    validate_save_target(target)?;
    fs::rename(temporary, target)
}

fn atomic_save_file_with<F>(target: &Path, bytes: &[u8], installer: F) -> io::Result<()>
where
    F: FnOnce(&Path, &Path) -> io::Result<()>,
{
    validate_save_target(target)?;
    let parent = target.parent().expect("validated save target has a parent");
    let (temporary, mut file) = create_save_temporary_file(parent)?;
    let mut guard = TemporaryFileGuard::new(temporary.clone());

    let result = (|| {
        file.write_all(bytes)?;
        file.flush()?;
        file.sync_all()?;
        drop(file);

        // Re-check immediately before installation so directories, symlinks,
        // and other non-regular destinations are never intentionally replaced.
        validate_save_target(target)?;
        installer(&temporary, target)
    })();

    match result {
        Ok(()) => {
            guard.disarm();
            Ok(())
        }
        Err(error) => match guard.cleanup() {
            Ok(()) => Err(error),
            Err(cleanup_error) => Err(io::Error::new(
                error.kind(),
                format!(
                    "{error}; additionally failed to remove save temporary file: {cleanup_error}"
                ),
            )),
        },
    }
}

async fn atomic_save_file(target: PathBuf, bytes: Vec<u8>) -> io::Result<()> {
    tokio::task::spawn_blocking(move || {
        atomic_save_file_with(&target, &bytes, install_save_temporary_file)
    })
    .await
    .map_err(|error| io::Error::other(format!("save task failed: {error}")))?
}

/// Save a file via a native save dialog.
///
/// Accepts either a `url` (fetched via GET) or raw `data` bytes.
/// WebView2 does not support blob-URL downloads triggered by `<a>.click()`,
/// so we handle file exports through Tauri IPC instead.
#[tauri::command]
pub async fn download_and_save(
    app: AppHandle,
    url: Option<String>,
    data: Option<Vec<u8>>,
    default_name: String,
) -> Result<bool, String> {
    use tauri_plugin_dialog::DialogExt;

    // Derive filter label + extension from the default filename
    let ext = default_name.rsplit('.').next().unwrap_or("*").to_string();
    let label = ext.to_uppercase();

    // Show native save dialog
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog()
        .file()
        .set_file_name(&default_name)
        .add_filter(&label, &[&ext])
        .save_file(move |path| {
            let _ = tx.send(path);
        });

    let file_path = rx.await.map_err(|e| format!("Dialog error: {e}"))?;
    let path = match file_path {
        Some(p) => p,
        None => return Ok(false), // User cancelled
    };

    let real_path = path
        .as_path()
        .ok_or_else(|| "Invalid save path".to_string())?;

    // Get bytes: from provided data or by downloading from URL
    let bytes = if let Some(raw) = data {
        raw
    } else if let Some(download_url) = url {
        let response = reqwest::get(&download_url)
            .await
            .map_err(|e| format!("Download failed: {e}"))?;
        response
            .bytes()
            .await
            .map_err(|e| format!("Failed to read response: {e}"))?
            .to_vec()
    } else {
        return Err("Either 'url' or 'data' must be provided".into());
    };

    atomic_save_file(real_path.to_path_buf(), bytes)
        .await
        .map_err(|e| format!("Failed to save file safely: {e}"))?;

    Ok(true)
}

/// Replace the tray's Recent Chats list with the given sessions (top first).
#[tauri::command]
pub fn update_tray_recents(app: AppHandle, recents: Vec<tray::TrayRecent>) -> Result<(), String> {
    tray::set_tray_recents(&app, &recents).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestDirectory {
        path: PathBuf,
    }

    impl TestDirectory {
        fn new(label: &str) -> Self {
            let sequence = SAVE_TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "suxiaoyou-save-test-{}-{label}-{sequence}",
                std::process::id()
            ));
            fs::create_dir(&path).expect("create test directory");
            Self { path }
        }

        fn path(&self) -> &Path {
            &self.path
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    fn temporary_entries(directory: &Path) -> Vec<PathBuf> {
        fs::read_dir(directory)
            .expect("read test directory")
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with(".suxiaoyou-save-"))
            })
            .collect()
    }

    #[test]
    fn temporary_files_are_unique_and_created_beside_the_target() {
        let directory = TestDirectory::new("unique");
        let (first_path, first_file) =
            create_save_temporary_file(directory.path()).expect("create first temporary file");
        let (second_path, second_file) =
            create_save_temporary_file(directory.path()).expect("create second temporary file");

        assert_ne!(first_path, second_path);
        assert_eq!(first_path.parent(), Some(directory.path()));
        assert_eq!(second_path.parent(), Some(directory.path()));

        drop(first_file);
        drop(second_file);
        fs::remove_file(first_path).expect("remove first temporary file");
        fs::remove_file(second_path).expect("remove second temporary file");
    }

    #[test]
    fn atomic_save_creates_a_new_file_and_removes_the_temporary_file() {
        let directory = TestDirectory::new("create");
        let target = directory.path().join("report.txt");

        atomic_save_file_with(&target, b"complete report", install_save_temporary_file)
            .expect("atomic save succeeds");

        assert_eq!(
            fs::read(&target).expect("read saved file"),
            b"complete report"
        );
        assert!(temporary_entries(directory.path()).is_empty());
    }

    #[test]
    fn atomic_save_replaces_an_existing_file() {
        let directory = TestDirectory::new("replace");
        let target = directory.path().join("report.txt");
        fs::write(&target, b"old report").expect("write original file");

        atomic_save_file_with(&target, b"new report", install_save_temporary_file)
            .expect("atomic replacement succeeds");

        assert_eq!(fs::read(&target).expect("read saved file"), b"new report");
        assert!(temporary_entries(directory.path()).is_empty());
    }

    #[test]
    fn install_failure_preserves_the_original_and_cleans_the_temporary_file() {
        let directory = TestDirectory::new("failure");
        let target = directory.path().join("report.txt");
        fs::write(&target, b"original report").expect("write original file");

        let error = atomic_save_file_with(&target, b"partial replacement", |temporary, path| {
            assert_eq!(path, target);
            assert_eq!(
                fs::read(temporary).expect("temporary file is fully written"),
                b"partial replacement"
            );
            Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "injected install failure",
            ))
        })
        .expect_err("injected installation failure is returned");

        assert_eq!(error.kind(), io::ErrorKind::PermissionDenied);
        assert_eq!(
            fs::read(&target).expect("read original file"),
            b"original report"
        );
        assert!(temporary_entries(directory.path()).is_empty());
    }

    #[test]
    fn atomic_save_rejects_directory_and_relative_targets_without_temporary_files() {
        let directory = TestDirectory::new("invalid");
        let target_directory = directory.path().join("folder");
        fs::create_dir(&target_directory).expect("create target directory");

        let directory_error =
            atomic_save_file_with(&target_directory, b"data", install_save_temporary_file)
                .expect_err("directory target is rejected");
        assert_eq!(directory_error.kind(), io::ErrorKind::InvalidInput);

        let relative_error = atomic_save_file_with(
            Path::new("relative-report.txt"),
            b"data",
            install_save_temporary_file,
        )
        .expect_err("relative target is rejected");
        assert_eq!(relative_error.kind(), io::ErrorKind::InvalidInput);
        assert!(temporary_entries(directory.path()).is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn atomic_save_rejects_a_symlink_without_changing_its_target() {
        use std::os::unix::fs::symlink;

        let directory = TestDirectory::new("symlink");
        let real_target = directory.path().join("real-report.txt");
        let link_target = directory.path().join("linked-report.txt");
        fs::write(&real_target, b"original report").expect("write symlink target");
        symlink(&real_target, &link_target).expect("create target symlink");

        let error =
            atomic_save_file_with(&link_target, b"replacement", install_save_temporary_file)
                .expect_err("symlink target is rejected");

        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert_eq!(
            fs::read(&real_target).expect("read symlink target"),
            b"original report"
        );
        assert!(temporary_entries(directory.path()).is_empty());
    }
}
