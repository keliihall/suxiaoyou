//! Tauri command handlers — the IPC bridge between frontend and Rust.

use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, WebviewWindow};
use tauri_plugin_opener::OpenerExt;
use tokio::io::AsyncWriteExt;

#[cfg(not(target_os = "windows"))]
use std::process::Stdio;

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

const NATIVE_SOURCE_INFO_ENDPOINT: &str = "/api/files/native-source-info";
const NATIVE_SOURCE_CONTENT_ENDPOINT: &str = "/api/files/native-source-content";

#[derive(Serialize)]
struct NativeSourceRequest<'a> {
    path: &'a str,
    session_id: &'a str,
}

#[derive(Deserialize)]
struct NativeSourceInfo {
    path: String,
    identity: String,
}

#[derive(Deserialize)]
struct BackendErrorBody {
    detail: Option<String>,
}

fn native_action_error(code: &str, detail: impl std::fmt::Display) -> String {
    format!("{code}:{detail}")
}

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

fn sanitized_default_file_name(value: &str) -> String {
    let basename = value.rsplit(['/', '\\']).next().unwrap_or_default().trim();
    let mut sanitized = String::with_capacity(basename.len().min(180));
    for character in basename.chars() {
        if sanitized.len() >= 180 {
            break;
        }
        if character.is_control()
            || matches!(
                character,
                '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*'
            )
        {
            sanitized.push('_');
        } else {
            sanitized.push(character);
        }
    }
    let sanitized = sanitized.trim_matches([' ', '.']);
    if sanitized.is_empty() {
        "download".to_string()
    } else {
        sanitized.to_string()
    }
}

fn save_dialog_extension(file_name: &str) -> Option<String> {
    let extension = Path::new(file_name).extension()?.to_str()?;
    (extension.len() <= 20
        && !extension.is_empty()
        && extension
            .chars()
            .all(|character| character.is_ascii_alphanumeric()))
    .then(|| extension.to_string())
}

fn sanitized_dialog_title(value: &str, fallback: &str) -> String {
    let title: String = value
        .chars()
        .filter(|character| !character.is_control())
        .take(120)
        .collect();
    let title = title.trim();
    if title.is_empty() {
        fallback.to_string()
    } else {
        title.to_string()
    }
}

async fn choose_save_target(
    app: &AppHandle,
    default_name: &str,
    dialog_title: &str,
) -> Result<Option<PathBuf>, String> {
    use tauri_plugin_dialog::DialogExt;

    let mut dialog = app
        .dialog()
        .file()
        .set_title(dialog_title)
        .set_file_name(default_name);
    if let Some(extension) = save_dialog_extension(default_name) {
        dialog = dialog.add_filter(extension.to_uppercase(), &[&extension]);
    }

    let (tx, rx) = tokio::sync::oneshot::channel();
    dialog.save_file(move |path| {
        let _ = tx.send(path);
    });
    let selected = rx
        .await
        .map_err(|error| native_action_error("dialog_failed", error))?;
    selected
        .map(|path| {
            path.as_path().map(Path::to_path_buf).ok_or_else(|| {
                native_action_error("invalid_target", "save target is not a local path")
            })
        })
        .transpose()
}

fn backend_error_code(status: reqwest::StatusCode) -> &'static str {
    match status.as_u16() {
        400 => "source_invalid",
        403 => "source_not_authorized",
        404 => "source_missing",
        _ => "backend_unavailable",
    }
}

async fn authorized_backend_response(
    state: &BackendState,
    endpoint: &str,
    source_path: &str,
    session_id: &str,
) -> Result<reqwest::Response, String> {
    let token = state
        .token()
        .await
        .map_err(|error| native_action_error("backend_unavailable", error))?;
    let base_url = state.url().await;
    let response = reqwest::Client::new()
        .post(format!("{}{}", base_url.trim_end_matches('/'), endpoint))
        .bearer_auth(token)
        .json(&NativeSourceRequest {
            path: source_path,
            session_id,
        })
        .send()
        .await
        .map_err(|error| native_action_error("backend_unavailable", error))?;

    if response.status().is_success() {
        return Ok(response);
    }

    let status = response.status();
    let detail = response
        .json::<BackendErrorBody>()
        .await
        .ok()
        .and_then(|body| body.detail)
        .unwrap_or_else(|| format!("backend returned HTTP {}", status.as_u16()));
    Err(native_action_error(backend_error_code(status), detail))
}

async fn authorized_source_info(
    state: &BackendState,
    source_path: &str,
    session_id: &str,
) -> Result<NativeSourceInfo, String> {
    authorized_backend_response(state, NATIVE_SOURCE_INFO_ENDPOINT, source_path, session_id)
        .await?
        .json::<NativeSourceInfo>()
        .await
        .map_err(|error| native_action_error("backend_unavailable", error))
}

fn ensure_same_authorized_source(
    expected: &NativeSourceInfo,
    current: &NativeSourceInfo,
) -> Result<(), String> {
    if expected.path == current.path && expected.identity == current.identity {
        Ok(())
    } else {
        Err(native_action_error(
            "source_changed",
            "source file changed before the native action",
        ))
    }
}

fn target_io_error(error: io::Error) -> String {
    let code = match error.kind() {
        io::ErrorKind::PermissionDenied => "permission_denied",
        io::ErrorKind::StorageFull | io::ErrorKind::QuotaExceeded => "disk_full",
        io::ErrorKind::NotFound => "target_missing",
        io::ErrorKind::InvalidInput => "invalid_target",
        _ => "save_failed",
    };
    native_action_error(code, error)
}

async fn stream_authorized_source_to_target(
    state: &BackendState,
    source_path: &str,
    session_id: &str,
    target: PathBuf,
) -> Result<(), String> {
    validate_save_target(&target).map_err(target_io_error)?;
    let mut response = authorized_backend_response(
        state,
        NATIVE_SOURCE_CONTENT_ENDPOINT,
        source_path,
        session_id,
    )
    .await?;
    let expected_length = response.content_length();

    let parent = target.parent().expect("validated save target has a parent");
    let (temporary, std_file) = create_save_temporary_file(parent).map_err(target_io_error)?;
    let mut guard = TemporaryFileGuard::new(temporary.clone());
    let mut file = tokio::fs::File::from_std(std_file);
    let mut copied = 0_u64;

    let streamed = async {
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| native_action_error("source_read_failed", error))?
        {
            file.write_all(&chunk).await.map_err(target_io_error)?;
            copied = copied.saturating_add(chunk.len() as u64);
        }
        if expected_length.is_some_and(|expected| expected != copied) {
            return Err(native_action_error(
                "source_read_failed",
                format!(
                    "source ended after {copied} of {} bytes",
                    expected_length.unwrap()
                ),
            ));
        }
        file.flush().await.map_err(target_io_error)?;
        file.sync_all().await.map_err(target_io_error)?;
        Ok(())
    }
    .await;
    drop(file);

    if let Err(error) = streamed {
        let _ = guard.cleanup();
        return Err(error);
    }

    let install_temporary = temporary.clone();
    let install_target = target.clone();
    let installed = tokio::task::spawn_blocking(move || {
        validate_save_target(&install_target)?;
        install_save_temporary_file(&install_temporary, &install_target)
    })
    .await
    .map_err(|error| native_action_error("save_failed", format!("save task failed: {error}")))?
    .map_err(target_io_error);

    match installed {
        Ok(()) => {
            guard.disarm();
            Ok(())
        }
        Err(error) => {
            let _ = guard.cleanup();
            Err(error)
        }
    }
}

#[cfg(not(target_os = "windows"))]
async fn choose_alternate_application(
    app: &AppHandle,
    dialog_title: &str,
) -> Result<Option<PathBuf>, String> {
    use tauri_plugin_dialog::DialogExt;

    let mut dialog = app.dialog().file().set_title(dialog_title);
    #[cfg(target_os = "macos")]
    {
        dialog = dialog
            .set_directory("/Applications")
            .add_filter("Applications", &["app"]);
    }
    #[cfg(target_os = "linux")]
    {
        let user_applications = std::env::var_os("HOME")
            .map(PathBuf::from)
            .map(|home| home.join(".local/share/applications"));
        let starting_directory = user_applications
            .filter(|path| path.is_dir())
            .unwrap_or_else(|| PathBuf::from("/usr/share/applications"));
        dialog = dialog.set_directory(starting_directory);
    }

    let (tx, rx) = tokio::sync::oneshot::channel();
    dialog.pick_file(move |path| {
        let _ = tx.send(path);
    });
    let selected = rx
        .await
        .map_err(|error| native_action_error("dialog_failed", error))?;
    selected
        .map(|path| {
            path.as_path().map(Path::to_path_buf).ok_or_else(|| {
                native_action_error("application_invalid", "application is not a local path")
            })
        })
        .transpose()
}

#[cfg(target_os = "macos")]
async fn launch_with_selected_application(
    application: &Path,
    state: &BackendState,
    source_path: &str,
    session_id: &str,
    expected_source: &NativeSourceInfo,
) -> Result<(), String> {
    let application = application
        .canonicalize()
        .map_err(|error| native_action_error("application_missing", error))?;
    let is_app_bundle = application.is_dir()
        && application
            .extension()
            .and_then(|extension| extension.to_str())
            .is_some_and(|extension| extension.eq_ignore_ascii_case("app"));
    if !is_app_bundle {
        return Err(native_action_error(
            "application_invalid",
            "select a macOS .app application",
        ));
    }

    // Validate the selected application first, then re-authorize the source
    // immediately before entering the path-based LaunchServices API.
    let current_source = authorized_source_info(state, source_path, session_id).await?;
    ensure_same_authorized_source(expected_source, &current_source)?;

    let status = tokio::process::Command::new("/usr/bin/open")
        .arg("-a")
        .arg(application)
        .arg(&current_source.path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map_err(|error| native_action_error("application_launch_failed", error))?;
    if status.success() {
        Ok(())
    } else {
        Err(native_action_error(
            "application_launch_failed",
            format!("macOS open exited with {status}"),
        ))
    }
}

#[cfg(target_os = "linux")]
async fn launch_with_selected_application(
    application: &Path,
    state: &BackendState,
    source_path: &str,
    session_id: &str,
    expected_source: &NativeSourceInfo,
) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    let application = application
        .canonicalize()
        .map_err(|error| native_action_error("application_missing", error))?;
    let metadata = application
        .metadata()
        .map_err(|error| native_action_error("application_missing", error))?;
    if !metadata.is_file() {
        return Err(native_action_error(
            "application_invalid",
            "selected application is not a regular file",
        ));
    }

    let is_desktop_entry = application
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| extension.eq_ignore_ascii_case("desktop"));
    if !is_desktop_entry && metadata.permissions().mode() & 0o111 != 0 {
        let current_source = authorized_source_info(state, source_path, session_id).await?;
        ensure_same_authorized_source(expected_source, &current_source)?;
        tokio::process::Command::new(&application)
            .arg(&current_source.path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| native_action_error("application_launch_failed", error))?;
        return Ok(());
    }
    if !is_desktop_entry {
        return Err(native_action_error(
            "application_invalid",
            "select a .desktop entry or executable application",
        ));
    }

    let current_source = authorized_source_info(state, source_path, session_id).await?;
    ensure_same_authorized_source(expected_source, &current_source)?;

    let status = tokio::process::Command::new("gio")
        .arg("launch")
        .arg(&application)
        .arg(&current_source.path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .await
        .map_err(|error| native_action_error("application_launch_failed", error))?;
    if status.success() {
        Ok(())
    } else {
        Err(native_action_error(
            "application_launch_failed",
            format!("application launcher exited with {status}"),
        ))
    }
}

#[cfg(target_os = "windows")]
fn launch_windows_open_with(source: &Path) -> Result<(), String> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};

    fn wide(value: &std::ffi::OsStr) -> Result<Vec<u16>, String> {
        let mut encoded: Vec<u16> = value.encode_wide().collect();
        if encoded.contains(&0) {
            return Err(native_action_error(
                "source_invalid",
                "source path contains a NUL character",
            ));
        }
        encoded.push(0);
        Ok(encoded)
    }

    let verb = wide(std::ffi::OsStr::new("openas"))?;
    let source = wide(source.as_os_str())?;
    let launched = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            verb.as_ptr(),
            source.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    if launched as isize > 32 {
        Ok(())
    } else {
        Err(native_action_error(
            "application_launch_failed",
            format!("Windows open-with returned code {}", launched as isize),
        ))
    }
}

/// Copy a session-authorized source through the backend's bounded byte stream.
/// The WebView never receives the file contents or a writable destination path.
#[tauri::command]
pub async fn save_authorized_file_as(
    app: AppHandle,
    state: tauri::State<'_, BackendState>,
    path: String,
    session_id: String,
    default_name: String,
    dialog_title: String,
) -> Result<bool, String> {
    let default_name = sanitized_default_file_name(&default_name);
    let dialog_title = sanitized_dialog_title(&dialog_title, "Save a copy");
    let Some(target) = choose_save_target(&app, &default_name, &dialog_title).await? else {
        return Ok(false);
    };
    stream_authorized_source_to_target(&state, &path, &session_id, target).await?;
    Ok(true)
}

/// Open a session-authorized source with a user-selected installed application.
///
/// Platform launchers accept a text path rather than the backend's authorized
/// handle.  We therefore compare the backend's opaque identity again after the
/// picker/application validation and immediately before launch.  This protects
/// the WebView trust boundary and detects replacements up to launcher entry; it
/// does not claim to defeat a hostile same-user process racing the final
/// path-based OS call.  Snapshot/fd paths are intentionally avoided because
/// they break edit-in-place and reveal-original-file semantics.
#[tauri::command]
pub async fn open_authorized_file_with(
    app: AppHandle,
    state: tauri::State<'_, BackendState>,
    path: String,
    session_id: String,
    dialog_title: String,
) -> Result<bool, String> {
    #[cfg(target_os = "windows")]
    {
        let _ = (&app, &dialog_title);
        let first_authorization = authorized_source_info(&state, &path, &session_id).await?;
        let current_authorization = authorized_source_info(&state, &path, &session_id).await?;
        ensure_same_authorized_source(&first_authorization, &current_authorization)?;
        // ShellExecuteW(openas) accepts only a path.  This second backend
        // authorization is intentionally adjacent to the launcher call.
        launch_windows_open_with(Path::new(&current_authorization.path))?;
        return Ok(true);
    }

    #[cfg(not(target_os = "windows"))]
    {
        // Reject an unauthorized/missing source before presenting a native
        // application picker that could otherwise imply the action is valid.
        let first_authorization = authorized_source_info(&state, &path, &session_id).await?;
        let dialog_title = sanitized_dialog_title(&dialog_title, "Choose an application");
        let Some(application) = choose_alternate_application(&app, &dialog_title).await? else {
            return Ok(false);
        };
        // Application validation happens inside the launcher helper before its
        // final source authorization, keeping that authorization adjacent to
        // the OS path launcher instead of before a potentially slow check.
        launch_with_selected_application(
            &application,
            &state,
            &path,
            &session_id,
            &first_authorization,
        )
        .await?;
        Ok(true)
    }
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
    fn native_dialog_values_cannot_smuggle_paths_or_control_characters() {
        assert_eq!(
            sanitized_default_file_name("../../reports/quarter:final?.pptx"),
            "quarter_final_.pptx"
        );
        assert_eq!(
            sanitized_default_file_name(r"C:\\Users\\Alex\\report.xlsx"),
            "report.xlsx"
        );
        assert_eq!(sanitized_default_file_name(".."), "download");
        assert_eq!(
            sanitized_dialog_title("  Save\nthis\tfile  ", "fallback"),
            "Savethisfile"
        );
        assert_eq!(sanitized_dialog_title("\n\t", "fallback"), "fallback");
    }

    #[test]
    fn native_open_with_rejects_a_changed_backend_identity() {
        let initial = NativeSourceInfo {
            path: "/workspace/report.txt".to_string(),
            identity: "v1:1:2:3:4:5".to_string(),
        };
        let unchanged = NativeSourceInfo {
            path: initial.path.clone(),
            identity: initial.identity.clone(),
        };
        let replaced = NativeSourceInfo {
            path: initial.path.clone(),
            identity: "v1:1:9:3:4:5".to_string(),
        };
        let redirected = NativeSourceInfo {
            path: "/workspace/other.txt".to_string(),
            identity: initial.identity.clone(),
        };

        assert!(ensure_same_authorized_source(&initial, &unchanged).is_ok());
        assert!(ensure_same_authorized_source(&initial, &replaced)
            .expect_err("changed identity must be rejected")
            .starts_with("source_changed:"));
        assert!(ensure_same_authorized_source(&initial, &redirected)
            .expect_err("changed canonical path must be rejected")
            .starts_with("source_changed:"));
    }

    #[test]
    fn native_save_filter_only_accepts_short_ascii_extensions() {
        assert_eq!(
            save_dialog_extension("report.PPTX").as_deref(),
            Some("PPTX")
        );
        assert_eq!(save_dialog_extension("README"), None);
        assert_eq!(save_dialog_extension("report.bad-ext"), None);
        assert_eq!(
            save_dialog_extension("report.thisextensioniswaytoolong"),
            None
        );
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
