//! Python backend lifecycle manager.
//!
//! Spawns the FastAPI backend as a child process, health-checks it,
//! and gracefully shuts it down on app exit. Includes watchdog for
//! detecting hung processes and auto-restart with exponential backoff.

use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{
    ffi::{OsStr, OsString},
    fs::OpenOptions,
    io::Write,
    path::{Path, PathBuf},
};

use log::{error, info, warn};
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::Mutex;
use tokio::time::sleep;

/// Watchdog polling interval.
const WATCHDOG_INTERVAL: Duration = Duration::from_secs(10);
/// How long to poll for the backend's session_token.json after /livez passes.
/// The backend writes the token before it starts the HTTP server, but we
/// read it in a separate step so we tolerate a short race window.
const TOKEN_MAX_WAIT: Duration = Duration::from_secs(5);
/// Interval between retries while waiting for the token file to appear.
const TOKEN_POLL_INTERVAL: Duration = Duration::from_millis(100);
/// Consecutive health failures before watchdog triggers a restart.
const WATCHDOG_MAX_FAILURES: u32 = 3;
/// Absolute maximum time to wait for health endpoint during startup.
/// Older Apple Silicon machines can need well over the previous 30-second
/// retry budget to unpack and import the PyInstaller runtime on first launch.
const HEALTH_STARTUP_TIMEOUT: Duration = Duration::from_secs(90);
/// Interval between health check retries during startup.
const HEALTH_RETRY_INTERVAL: Duration = Duration::from_millis(500);
/// Timeout for graceful shutdown before force-killing.
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(12);
/// Maximum auto-restart attempts within the crash window.
const MAX_CRASH_RESTARTS: u32 = 3;
/// Reset crash count after this many ms of stability.
const CRASH_WINDOW_MS: u64 = 60_000;
/// Maximum time allowed for force termination and parent-process reaping.
const PROCESS_KILL_TIMEOUT: Duration = Duration::from_secs(5);
/// Maximum time allowed for Windows `taskkill /T /F`.
#[cfg(target_os = "windows")]
const TASKKILL_TIMEOUT: Duration = Duration::from_secs(5);
/// Maximum user-facing failure detail carried by a lifecycle snapshot.
const MAX_STATUS_DETAIL_CHARS: usize = 2_000;
/// Prefix shared by all per-run desktop session tokens.
const SESSION_TOKEN_PREFIX: &str = "suxiaoyou_st_";

/// Stable lifecycle phases exposed to the desktop frontend.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendPhase {
    Initializing,
    Ready,
    Restarting,
    Failed,
}

/// Serializable snapshot of the native backend lifecycle.
///
/// Every transition increments `revision`. The frontend subscribes before
/// fetching this snapshot and ignores an older revision, which closes the
/// event/snapshot race without making provider configuration part of native
/// process health.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct BackendStatus {
    pub revision: u64,
    pub phase: BackendPhase,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_attempts: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

impl BackendStatus {
    fn initial() -> Self {
        Self {
            revision: 0,
            phase: BackendPhase::Initializing,
            url: None,
            attempt: None,
            max_attempts: None,
            failure_code: None,
            detail: None,
        }
    }
}

struct StatusUpdate {
    phase: BackendPhase,
    url: Option<String>,
    attempt: Option<u32>,
    max_attempts: Option<u32>,
    failure_code: Option<String>,
    detail: Option<String>,
}

impl StatusUpdate {
    fn initializing() -> Self {
        Self {
            phase: BackendPhase::Initializing,
            url: None,
            attempt: None,
            max_attempts: None,
            failure_code: None,
            detail: None,
        }
    }

    fn ready(url: String) -> Self {
        Self {
            phase: BackendPhase::Ready,
            url: Some(url),
            attempt: None,
            max_attempts: None,
            failure_code: None,
            detail: None,
        }
    }

    fn restarting(url: Option<String>, attempt: u32) -> Self {
        Self {
            phase: BackendPhase::Restarting,
            url,
            attempt: Some(attempt),
            max_attempts: Some(MAX_CRASH_RESTARTS),
            failure_code: None,
            detail: None,
        }
    }

    fn failed(failure_code: &str, detail: &str) -> Self {
        Self {
            phase: BackendPhase::Failed,
            url: None,
            attempt: None,
            max_attempts: None,
            failure_code: Some(failure_code.to_string()),
            detail: Some(detail.to_string()),
        }
    }
}

/// Windows CREATE_NEW_PROCESS_GROUP flag.
#[cfg(target_os = "windows")]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
/// Keep the console-mode PyInstaller backend hidden while preserving pipes.
#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn epoch_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn predecessor_data_dir(app_data_dir: &Path) -> Option<PathBuf> {
    let data_root = app_data_dir.parent()?;
    let predecessor_identifier = format!("com.{}.desktop", concat!("open", "yak"));
    Some(data_root.join(predecessor_identifier).join("data"))
}

fn bundled_node_bin_dir(resource_dir: &Path) -> PathBuf {
    let runtime_root = resource_dir.join("nodejs");
    if cfg!(target_os = "windows") {
        runtime_root
    } else {
        runtime_root.join("bin")
    }
}

fn prepend_runtime_path(bin_dir: &Path, current: Option<&OsStr>) -> Result<OsString, String> {
    let same_path = |candidate: &Path| {
        if cfg!(target_os = "windows") {
            candidate
                .to_string_lossy()
                .eq_ignore_ascii_case(&bin_dir.to_string_lossy())
        } else {
            candidate == bin_dir
        }
    };

    let mut paths = vec![bin_dir.to_path_buf()];
    if let Some(current) = current {
        paths.extend(std::env::split_paths(current).filter(|path| !same_path(path)));
    }
    std::env::join_paths(paths).map_err(|err| format!("Failed to construct backend PATH: {err}"))
}

fn next_backend_status(
    revision: u64,
    mut update: StatusUpdate,
    known_session_token: Option<&str>,
) -> BackendStatus {
    update.detail = update
        .detail
        .map(|detail| sanitize_status_detail(&detail, known_session_token));

    BackendStatus {
        revision: revision.saturating_add(1),
        phase: update.phase,
        url: update.url,
        attempt: update.attempt,
        max_attempts: update.max_attempts,
        failure_code: update.failure_code,
        detail: update.detail.filter(|detail| !detail.is_empty()),
    }
}

fn sanitize_status_detail(detail: &str, known_session_token: Option<&str>) -> String {
    let mut redacted = detail.to_string();
    if let Some(token) = known_session_token.filter(|token| !token.is_empty()) {
        redacted = redacted.replace(token, "[redacted]");
    }
    redacted = redact_prefixed_tokens(&redacted);

    if redacted.chars().count() <= MAX_STATUS_DETAIL_CHARS {
        redacted
    } else {
        let mut truncated: String = redacted
            .chars()
            .take(MAX_STATUS_DETAIL_CHARS.saturating_sub(1))
            .collect();
        truncated.push('…');
        truncated
    }
}

fn redact_prefixed_tokens(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    let mut remaining = input;

    while let Some(start) = remaining.find(SESSION_TOKEN_PREFIX) {
        output.push_str(&remaining[..start]);
        output.push_str("[redacted]");

        let token_tail = &remaining[start + SESSION_TOKEN_PREFIX.len()..];
        let token_end = token_tail
            .char_indices()
            .find_map(|(index, ch)| {
                (!matches!(ch, 'a'..='z' | 'A'..='Z' | '0'..='9' | '_' | '-')).then_some(index)
            })
            .unwrap_or(token_tail.len());
        remaining = &token_tail[token_end..];
    }

    output.push_str(remaining);
    output
}

/// Shared backend state managed by Tauri.
pub struct BackendState {
    inner: Arc<Mutex<BackendInner>>,
}

struct BackendInner {
    status: BackendStatus,
    port: u16,
    process: Option<Child>,
    /// Monotonically changing identity of the child stored in `process`.
    /// Monitors may only inspect or detach the generation they were created for.
    generation: u64,
    intentional_stop: bool,
    watchdog_generation: Option<u64>,
    /// Restart coordination is independent from the user-facing status phase.
    restart_in_progress: bool,
    crash_count: u32,
    last_crash_time: u64,
    /// Session bearer token loaded from the backend's session_token.json.
    /// Cleared on restart and repopulated from disk after the new backend
    /// passes its health check — never sent over the wire by the backend,
    /// only written to a 0600 file we read directly.
    session_token: Option<String>,
    /// Data directory the backend writes its session token into. Set at
    /// spawn (prod) or via set_dev_data_dir (dev) so token refresh after
    /// an auto-restart uses the same path.
    data_dir: Option<PathBuf>,
}

fn next_process_generation(current: u64) -> u64 {
    let next = current.wrapping_add(1);
    if next == 0 {
        1
    } else {
        next
    }
}

fn owns_process_generation(inner: &BackendInner, generation: u64) -> bool {
    inner.generation == generation && inner.process.is_some()
}

/// Detach only the child owned by `generation`. A stale monitor must never
/// clear credentials or take a newer child that reused the shared slot.
fn take_process_for_generation(inner: &mut BackendInner, generation: u64) -> Option<Child> {
    if !owns_process_generation(inner, generation) {
        return None;
    }
    inner.session_token = None;
    inner.process.take()
}

fn clear_watchdog_generation(inner: &mut BackendInner, generation: u64) {
    if inner.watchdog_generation == Some(generation) {
        inner.watchdog_generation = None;
    }
}

fn append_cleanup_error(primary: String, cleanup_error: Option<String>) -> String {
    match cleanup_error {
        Some(cleanup) => format!("{primary}; cleanup also failed: {cleanup}"),
        None => primary,
    }
}

impl BackendState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(BackendInner {
                status: BackendStatus::initial(),
                port: 0,
                process: None,
                generation: 0,
                intentional_stop: false,
                watchdog_generation: None,
                restart_in_progress: false,
                crash_count: 0,
                last_crash_time: 0,
                session_token: None,
                data_dir: None,
            })),
        }
    }

    /// Return the latest lifecycle snapshot for race-free frontend bootstrap.
    pub async fn status(&self) -> BackendStatus {
        self.inner.lock().await.status.clone()
    }

    /// Publish the cold-start state once a Tauri app handle is available.
    pub async fn publish_initializing(&self, app: &AppHandle) -> BackendStatus {
        self.publish_status(app, StatusUpdate::initializing()).await
    }

    /// Publish readiness for the externally-managed development backend.
    pub async fn publish_dev_ready(&self, app: &AppHandle) -> BackendStatus {
        let url = self.url().await;
        self.publish_status(app, StatusUpdate::ready(url)).await
    }

    /// Publish a terminal backend failure. Details are redacted and bounded
    /// before they cross the IPC boundary.
    pub async fn publish_failed(
        &self,
        app: &AppHandle,
        failure_code: &str,
        detail: &str,
    ) -> BackendStatus {
        self.publish_status(app, StatusUpdate::failed(failure_code, detail))
            .await
    }

    async fn publish_ready(&self, app: &AppHandle, url: String) -> BackendStatus {
        self.publish_status(app, StatusUpdate::ready(url)).await
    }

    async fn publish_restarting(&self, app: &AppHandle, attempt: u32) -> BackendStatus {
        let url = {
            let inner = self.inner.lock().await;
            (inner.port > 0).then(|| format!("http://127.0.0.1:{}", inner.port))
        };
        self.publish_status(app, StatusUpdate::restarting(url, attempt))
            .await
    }

    async fn publish_status(&self, app: &AppHandle, update: StatusUpdate) -> BackendStatus {
        let snapshot = {
            let mut inner = self.inner.lock().await;
            let next = next_backend_status(
                inner.status.revision,
                update,
                inner.session_token.as_deref(),
            );
            if !matches!(next.phase, BackendPhase::Ready) {
                inner.session_token = None;
            }
            inner.status = next.clone();
            next
        };

        if let Err(err) = app.emit("backend-status", &snapshot) {
            warn!(
                "Failed to emit backend-status revision {}: {err}",
                snapshot.revision
            );
        }
        snapshot
    }

    /// Returns the backend URL (http://127.0.0.1:{port}).
    pub async fn url(&self) -> String {
        let inner = self.inner.lock().await;
        format!("http://127.0.0.1:{}", inner.port)
    }

    /// Set port for dev mode (backend already running externally).
    pub async fn set_dev_port(&self, port: u16) {
        let mut inner = self.inner.lock().await;
        inner.port = port;
    }

    /// Set the backend data directory in dev mode and load its session
    /// token. In dev the backend is started externally by `dev-desktop.mjs`
    /// so there is no spawn to hook into; we poll the well-known token
    /// path instead. Returns the token on success.
    pub async fn set_dev_data_dir(&self, data_dir: PathBuf) -> Result<String, String> {
        {
            let mut inner = self.inner.lock().await;
            inner.data_dir = Some(data_dir.clone());
        }
        let token = load_session_token(&data_dir).await?;
        let mut inner = self.inner.lock().await;
        inner.session_token = Some(token.clone());
        Ok(token)
    }

    /// Returns the backend session bearer token, or an error if the
    /// backend has not yet written it. The frontend calls this through
    /// the `get_backend_token` Tauri command and attaches the value to
    /// every HTTP request.
    pub async fn token(&self) -> Result<String, String> {
        let inner = self.inner.lock().await;
        inner
            .session_token
            .clone()
            .ok_or_else(|| "Backend session token not yet available".to_string())
    }

    /// Start the Python backend process.
    pub async fn start(&self, app: &AppHandle) -> Result<String, String> {
        let mut inner = self.inner.lock().await;

        if inner.intentional_stop {
            return Err("Backend shutdown is in progress".to_string());
        }
        if inner.process.is_some() {
            return Err("Backend process is already running".to_string());
        }

        // Port selection: reuse previous port if available, otherwise pick a new one.
        // This avoids stale-URL windows on restart — the frontend already has the right URL cached.
        let previous_port = inner.port;
        let port = if previous_port > 0 && portpicker::is_free(previous_port) {
            previous_port
        } else {
            portpicker::pick_unused_port().ok_or("No available port")?
        };

        inner.port = port;
        inner.session_token = None;

        // Determine binary path and data directory
        let is_dev = cfg!(debug_assertions);

        let app_data_dir = app
            .path()
            .app_data_dir()
            .map_err(|e| format!("Failed to get app data dir: {e}"))?;
        let data_dir = app_data_dir.join("data");
        let legacy_data_dir = predecessor_data_dir(&app_data_dir);

        // Ensure data directory exists
        std::fs::create_dir_all(&data_dir)
            .map_err(|e| format!("Failed to create data dir: {e}"))?;

        // Set up log file
        let log_dir = app
            .path()
            .app_log_dir()
            .map_err(|e| format!("Failed to get log dir: {e}"))?;
        std::fs::create_dir_all(&log_dir).map_err(|e| format!("Failed to create log dir: {e}"))?;
        let log_path = log_dir.join("backend.log");
        let desktop_log_path = log_dir.join("desktop.log");
        write_desktop_log(
            &desktop_log_path,
            &format!(
                "Starting desktop backend | app_version={} | data_dir={} | log_dir={}",
                app.package_info().version,
                data_dir.display(),
                log_dir.display()
            ),
        );

        let mut child = if is_dev {
            // Development: use system Python
            let backend_dir = std::env::current_dir()
                .map_err(|e| format!("Failed to get cwd: {e}"))?
                .parent()
                .ok_or("No parent dir")?
                .join("backend");

            info!(
                "Starting backend (dev) at {}:{}",
                backend_dir.display(),
                port
            );
            write_desktop_log(
                &desktop_log_path,
                &format!(
                    "Dev backend startup | backend_dir={} | port={}",
                    backend_dir.display(),
                    port
                ),
            );

            let mut cmd = Command::new("python");
            cmd.kill_on_drop(true);
            cmd.args([
                "-m",
                "uvicorn",
                "app.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                &port.to_string(),
            ])
            .current_dir(&backend_dir)
            .env("PYTHONUNBUFFERED", "1")
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .stdin(Stdio::null());

            // Windows: isolate process group so CTRL_BREAK_EVENT doesn't leak to Tauri
            #[cfg(target_os = "windows")]
            cmd.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
            // Unix: put the backend and inherited descendants in their own
            // process group so cleanup cannot leave helper processes behind.
            #[cfg(unix)]
            cmd.process_group(0);

            cmd.spawn()
                .map_err(|e| format!("Failed to spawn backend: {e}"))?
        } else {
            // Production: use PyInstaller binary from resources
            let resource_dir = app
                .path()
                .resource_dir()
                .map_err(|e| format!("Failed to get resource dir: {e}"))?;
            let backend_binary = if cfg!(target_os = "windows") {
                "suxiaoyou-backend.exe"
            } else {
                "suxiaoyou-backend"
            };
            let backend_path = resource_dir.join("backend").join(backend_binary);
            let backend_dir = resource_dir.join("backend");

            info!("Starting backend (prod) at {}", backend_path.display());
            write_desktop_log(
                &desktop_log_path,
                &format!(
                    "Prod backend startup | resource_dir={} | backend_dir={} | backend_path={} | port={}",
                    resource_dir.display(),
                    backend_dir.display(),
                    backend_path.display(),
                    port
                ),
            );
            validate_backend_resources(&backend_dir, &backend_path, &desktop_log_path)?;

            let node_bin_dir = bundled_node_bin_dir(&resource_dir);
            let backend_path_env =
                prepend_runtime_path(&node_bin_dir, std::env::var_os("PATH").as_deref())?;

            let mut cmd = Command::new(&backend_path);
            cmd.kill_on_drop(true);
            cmd.arg("--port")
                .arg(port.to_string())
                .arg("--data-dir")
                .arg(&data_dir)
                .arg("--resource-dir")
                .arg(&resource_dir);
            if let Some(legacy_data_dir) = legacy_data_dir.as_ref() {
                cmd.arg("--legacy-data-dir").arg(legacy_data_dir);
            }
            cmd.env("PYTHONUNBUFFERED", "1")
                .env("PATH", backend_path_env)
                .env("SUXIAOYOU_NODE_BIN_DIR", &node_bin_dir)
                .env(
                    "SUXIAOYOU_DESKTOP_PARENT_PID",
                    std::process::id().to_string(),
                )
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .stdin(Stdio::null());

            // Windows: isolate process group so CTRL_BREAK_EVENT doesn't leak to Tauri
            #[cfg(target_os = "windows")]
            cmd.creation_flags(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
            #[cfg(unix)]
            cmd.process_group(0);

            cmd.spawn()
                .map_err(|e| format!("Failed to spawn backend: {e}"))?
        };

        // Take stdout/stderr before storing child (to avoid partial move)
        let child_stdout = child.stdout.take();
        let child_stderr = child.stderr.take();

        // Store the process under a new ownership generation. Any monitor from
        // an older generation becomes observational only and exits without
        // touching this child.
        inner.generation = next_process_generation(inner.generation);
        let generation = inner.generation;
        inner.process = Some(child);

        drop(inner); // Release lock before waiting for health

        // Pipe output to log file in background
        if let Some(stdout) = child_stdout {
            let log_path_clone = log_path.clone();
            tokio::spawn(async move {
                let reader = BufReader::new(stdout);
                let mut lines = reader.lines();
                let file = tokio::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&log_path_clone)
                    .await;
                if let Ok(mut file) = file {
                    while let Ok(Some(line)) = lines.next_line().await {
                        let _ = file.write_all(format!("{line}\n").as_bytes()).await;
                    }
                }
            });
        }
        if let Some(stderr) = child_stderr {
            let log_path_clone = log_path.clone();
            tokio::spawn(async move {
                let reader = BufReader::new(stderr);
                let mut lines = reader.lines();
                let file = tokio::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&log_path_clone)
                    .await;
                if let Ok(mut file) = file {
                    while let Ok(Some(line)) = lines.next_line().await {
                        let _ = file
                            .write_all(format!("[stderr] {line}\n").as_bytes())
                            .await;
                    }
                }
            });
        }

        // Wait for backend health check (use lightweight /livez endpoint)
        if let Err(err) = self
            .wait_for_health(port, generation, &log_path, &desktop_log_path)
            .await
        {
            let cleanup_error = self.cleanup_failed_start(generation).await.err();
            return Err(append_cleanup_error(err, cleanup_error));
        }

        // Load the session token the backend wrote on startup. Every
        // authenticated request from the frontend carries it as a
        // bearer; without it the backend rejects us. We poll briefly
        // to tolerate the race where /livez is up before the token
        // file has been flushed.
        let token_data_dir = data_dir.clone();
        let token = match load_session_token(&token_data_dir).await {
            Ok(token) => token,
            Err(err) => {
                let msg = format!("Session token unavailable: {err}");
                write_desktop_log(&desktop_log_path, &msg);
                let cleanup_error = self.cleanup_failed_start(generation).await.err();
                return Err(append_cleanup_error(msg, cleanup_error));
            }
        };

        // Health and token availability are separate gates. Confirm the same
        // child is still alive before making Ready/token visible atomically.
        if let Some(err) = self.startup_process_error(generation).await {
            let cleanup_error = self.cleanup_failed_start(generation).await.err();
            return Err(append_cleanup_error(err, cleanup_error));
        }

        // Cache the token and data_dir so a later restart can refresh the
        // token from disk. Crash counts intentionally survive a successful
        // restart until the stability window elapses, preventing rapid crash
        // loops from retrying forever.
        let committed = {
            let mut inner = self.inner.lock().await;
            if owns_process_generation(&inner, generation) && !inner.intentional_stop {
                inner.session_token = Some(token);
                inner.data_dir = Some(token_data_dir);
                true
            } else {
                false
            }
        };
        if !committed {
            let msg = "Backend ownership changed before startup completed".to_string();
            let cleanup_error = self.cleanup_failed_start(generation).await.err();
            return Err(append_cleanup_error(msg, cleanup_error));
        }

        // Start watchdog
        self.start_watchdog(app.clone(), generation);

        // Start process exit monitor — detects crashes faster than the watchdog
        self.start_exit_monitor(app.clone(), generation);

        let url = format!("http://127.0.0.1:{port}");
        info!("Backend ready at {url}");
        write_desktop_log(&desktop_log_path, &format!("Backend ready at {url}"));
        self.publish_ready(app, url.clone()).await;
        Ok(url)
    }

    /// Wait for the backend /livez endpoint to respond.
    async fn wait_for_health(
        &self,
        port: u16,
        generation: u64,
        backend_log_path: &Path,
        desktop_log_path: &Path,
    ) -> Result<(), String> {
        let url = format!("http://127.0.0.1:{port}/livez");
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .map_err(|e| format!("Failed to create HTTP client: {e}"))?;

        let started = Instant::now();
        let deadline = started + HEALTH_STARTUP_TIMEOUT;
        let mut attempt = 0_u32;

        loop {
            if let Some(exit_error) = self.startup_process_error(generation).await {
                write_desktop_log(desktop_log_path, &exit_error);
                return Err(exit_error);
            }

            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }

            attempt += 1;
            match client
                .get(&url)
                .timeout(remaining.min(Duration::from_secs(2)))
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    info!(
                        "Backend health check passed (attempt {}, elapsed={}ms)",
                        attempt,
                        started.elapsed().as_millis()
                    );
                    write_desktop_log(
                        desktop_log_path,
                        &format!(
                            "Backend health check passed | attempt={} | elapsed_ms={} | url={}",
                            attempt,
                            started.elapsed().as_millis(),
                            url
                        ),
                    );
                    return Ok(());
                }
                _ => {}
            }

            // Check again immediately after a failed request. `Child` remains
            // `Some` after exit until `try_wait()` reaps it, so checking only
            // the option would otherwise burn the full 90-second budget.
            if let Some(exit_error) = self.startup_process_error(generation).await {
                write_desktop_log(desktop_log_path, &exit_error);
                return Err(exit_error);
            }

            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            sleep(remaining.min(HEALTH_RETRY_INTERVAL)).await;
        }

        let recent_backend_log = read_recent_log_lines(backend_log_path, 20);
        let operator_message = format!(
            "Backend did not become ready after {}ms. Recent backend log:\n{}",
            HEALTH_STARTUP_TIMEOUT.as_millis(),
            recent_backend_log
        );
        write_desktop_log(desktop_log_path, &operator_message);
        Err(format!(
            "Backend did not become ready after {}ms. Open the log directory for details.",
            HEALTH_STARTUP_TIMEOUT.as_millis()
        ))
    }

    /// Reap an exited startup child and return a diagnostic immediately.
    async fn startup_process_error(&self, generation: u64) -> Option<String> {
        let (mut diagnostic, child_to_kill, residual_group) = {
            let mut inner = self.inner.lock().await;
            if inner.generation != generation {
                return Some(format!(
                    "Backend process ownership changed during startup (expected generation {generation}, current generation {})",
                    inner.generation
                ));
            }
            let process_group = inner.process.as_ref().and_then(Child::id);
            let result = match inner.process.as_mut() {
                Some(child) => child.try_wait(),
                None => return Some("Backend process is unavailable before becoming ready".into()),
            };

            match result {
                Ok(Some(status)) => {
                    let _ = take_process_for_generation(&mut inner, generation);
                    (
                        Some(format!(
                            "Backend process exited before becoming ready (status: {status})"
                        )),
                        None,
                        process_group,
                    )
                }
                Ok(None) => (None, None, None),
                Err(err) => {
                    let child = take_process_for_generation(&mut inner, generation);
                    (
                        Some(format!(
                            "Failed to inspect backend process during startup: {err}"
                        )),
                        child,
                        None,
                    )
                }
            }
        };

        if let Some(mut child) = child_to_kill {
            if let Err(kill_err) = kill_process_tree(&mut child).await {
                if let Some(message) = diagnostic.as_mut() {
                    message.push_str(&format!("; cleanup also failed: {kill_err}"));
                }
            }
        } else if let Err(kill_err) = terminate_residual_process_group(residual_group) {
            if let Some(message) = diagnostic.as_mut() {
                message.push_str(&format!("; descendant cleanup also failed: {kill_err}"));
            }
        }
        diagnostic
    }

    /// Ensure a failed health/token startup cannot leave an unmanaged child.
    async fn cleanup_failed_start(&self, generation: u64) -> Result<(), String> {
        let child = {
            let mut inner = self.inner.lock().await;
            take_process_for_generation(&mut inner, generation)
        };

        if let Some(mut child) = child {
            kill_process_tree(&mut child).await?;
        }
        Ok(())
    }

    /// Start the watchdog that monitors backend health.
    fn start_watchdog(&self, app: AppHandle, generation: u64) {
        let inner = self.inner.clone();

        tokio::spawn(async move {
            let client = reqwest::Client::builder()
                .timeout(Duration::from_secs(5))
                .build()
                .unwrap();

            let mut consecutive_failures: u32 = 0;

            {
                let mut guard = inner.lock().await;
                if guard.intentional_stop || !owns_process_generation(&guard, generation) {
                    return;
                }
                if guard.watchdog_generation == Some(generation) {
                    return;
                }
                guard.watchdog_generation = Some(generation);
            }

            loop {
                sleep(WATCHDOG_INTERVAL).await;

                let guard = inner.lock().await;
                if guard.intentional_stop
                    || guard.watchdog_generation != Some(generation)
                    || !owns_process_generation(&guard, generation)
                {
                    break;
                }
                let port = guard.port;
                drop(guard);

                // Use lightweight /livez endpoint — no external API calls
                let url = format!("http://127.0.0.1:{port}/livez");
                match client.get(&url).send().await {
                    Ok(resp) if resp.status().is_success() => {
                        consecutive_failures = 0;
                    }
                    _ => {
                        consecutive_failures += 1;
                        warn!(
                            "Watchdog: health check failed ({}/{})",
                            consecutive_failures, WATCHDOG_MAX_FAILURES
                        );

                        if consecutive_failures >= WATCHDOG_MAX_FAILURES {
                            error!("Watchdog: backend unresponsive, forcing restart");

                            // Kill the hung process
                            let child = {
                                let mut guard = inner.lock().await;
                                if guard.intentional_stop
                                    || guard.watchdog_generation != Some(generation)
                                {
                                    None
                                } else {
                                    clear_watchdog_generation(&mut guard, generation);
                                    take_process_for_generation(&mut guard, generation)
                                }
                            };
                            let Some(mut child) = child else {
                                break;
                            };

                            let state = app.state::<BackendState>();
                            if let Err(err) = kill_process_tree(&mut child).await {
                                let msg = format!(
                                    "Watchdog could not terminate backend generation {generation}: {err}"
                                );
                                error!("{msg}");
                                let snapshot = state
                                    .publish_failed(&app, "backend_cleanup_failed", &msg)
                                    .await;
                                let _ = app.emit(
                                    "backend-crash",
                                    snapshot.detail.as_deref().unwrap_or(
                                        "Backend cleanup failed; automatic restart was stopped",
                                    ),
                                );
                                break;
                            }

                            // Attempt auto-restart only after the old process
                            // tree has been confirmed terminated and reaped.
                            attempt_restart(&state, &app).await;
                            break;
                        }
                    }
                }
            }

            let mut guard = inner.lock().await;
            clear_watchdog_generation(&mut guard, generation);
        });
    }

    /// Monitor the child process for unexpected exits.
    /// Detects crashes immediately instead of waiting for watchdog polling.
    fn start_exit_monitor(&self, app: AppHandle, generation: u64) {
        let inner = self.inner.clone();

        tokio::spawn(async move {
            // We need to call child.wait() but the child is behind a Mutex.
            // Poll periodically using try_wait semantics by checking child.id().
            loop {
                sleep(Duration::from_secs(2)).await;

                let observation = {
                    let mut guard = inner.lock().await;
                    if guard.intentional_stop || !owns_process_generation(&guard, generation) {
                        return;
                    }

                    let process_group = guard.process.as_ref().and_then(Child::id);
                    let result = guard
                        .process
                        .as_mut()
                        .expect("generation ownership requires a process")
                        .try_wait();
                    match result {
                        Ok(Some(status)) => {
                            let child = take_process_for_generation(&mut guard, generation);
                            Some((status.to_string(), child, None, process_group))
                        }
                        Ok(None) => None,
                        Err(err) => {
                            let child = take_process_for_generation(&mut guard, generation);
                            Some((
                                "unknown".to_string(),
                                child,
                                Some(err.to_string()),
                                process_group,
                            ))
                        }
                    }
                };

                let Some((status, child, inspection_error, process_group)) = observation else {
                    continue;
                };
                let Some(mut child) = child else {
                    return;
                };

                if let Some(inspection_error) = inspection_error {
                    warn!(
                        "Exit monitor: failed to inspect backend generation {generation}: {inspection_error}"
                    );
                    if let Err(kill_error) = kill_process_tree(&mut child).await {
                        let state = app.state::<BackendState>();
                        let msg = format!(
                            "Backend inspection failed ({inspection_error}) and cleanup failed ({kill_error})"
                        );
                        error!("{msg}");
                        let snapshot = state
                            .publish_failed(&app, "backend_cleanup_failed", &msg)
                            .await;
                        let _ = app.emit(
                            "backend-crash",
                            snapshot
                                .detail
                                .as_deref()
                                .unwrap_or("Backend cleanup failed"),
                        );
                        return;
                    }
                } else {
                    // `try_wait()` already reaped the exited parent. Dropping
                    // this now-empty handle is safe and cannot affect a newer generation.
                    warn!("Exit monitor: backend exited with status {status}");
                    if let Err(kill_error) = terminate_residual_process_group(process_group) {
                        let state = app.state::<BackendState>();
                        let msg =
                            format!("Backend exited, but descendant cleanup failed: {kill_error}");
                        error!("{msg}");
                        let snapshot = state
                            .publish_failed(&app, "backend_cleanup_failed", &msg)
                            .await;
                        let _ = app.emit(
                            "backend-crash",
                            snapshot
                                .detail
                                .as_deref()
                                .unwrap_or("Backend descendant cleanup failed"),
                        );
                        return;
                    }
                }

                let state = app.state::<BackendState>();
                attempt_restart(&state, &app).await;
                return;
            }
        });
    }

    /// Stop the backend process gracefully.
    pub async fn stop(&self) -> Result<(), String> {
        let (port, token, child) = {
            let mut inner = self.inner.lock().await;
            inner.intentional_stop = true;
            inner.restart_in_progress = false;
            inner.watchdog_generation = None;

            let port = inner.port;
            // Clone the token while we hold the lock so the HTTP shutdown
            // below can authenticate. Without a bearer token the backend
            // rejects /shutdown exactly like any other mutating request.
            let token = inner.session_token.take();
            let child = inner.process.take();
            (port, token, child)
        };

        let Some(mut child) = child else {
            return Ok(());
        };

        info!("Stopping backend (port={port})...");

        // Step 1: Try HTTP graceful shutdown. The child has been taken from
        // shared state, so monitors stop and no mutex is held during I/O.
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap_or_default();

        let shutdown_url = format!("http://127.0.0.1:{port}/shutdown");
        let mut req = client.post(&shutdown_url);
        if let Some(t) = token.as_deref() {
            req = req.bearer_auth(t);
        }
        match req.send().await {
            Ok(resp) if resp.status().is_success() => {
                info!("Shutdown request accepted");
            }
            Ok(resp) => {
                warn!(
                    "Shutdown request rejected (status={}), will force kill",
                    resp.status()
                );
            }
            Err(_) => {
                warn!("HTTP shutdown failed, will force kill");
            }
        }

        // Step 2: Wait for process to exit
        let process_group = child.id();
        let wait_result = tokio::time::timeout(SHUTDOWN_TIMEOUT, child.wait()).await;

        match wait_result {
            Ok(Ok(status)) => {
                info!("Backend exited with status: {status}");
                terminate_residual_process_group(process_group)?;
            }
            _ => {
                // Step 3: Force kill
                warn!("Backend did not exit gracefully, force killing...");
                kill_process_tree(&mut child).await?;
            }
        }
        Ok(())
    }
}

/// Preserve the legacy event without sending arbitrary backend log contents
/// (which can contain workspace paths, prompts, or provider diagnostics) into
/// the renderer. Detailed logs remain available through the fixed log folder.
fn emit_crash_notice(app: &AppHandle) {
    let _ = app.emit(
        "backend-crash-log",
        "Backend exited unexpectedly. Open the application log directory for details.",
    );
}

/// Attempt to restart the backend with exponential backoff.
/// Emits `backend-restart` on success or `backend-crash` on final failure.
async fn attempt_restart(state: &BackendState, app: &AppHandle) {
    emit_crash_notice(app);

    let mut attempt = {
        let mut inner = state.inner.lock().await;
        if inner.intentional_stop || inner.restart_in_progress {
            return; // Another restart already in progress, or intentional stop
        }

        let now = epoch_ms();
        // Reset crash counter if stable for a while
        if now.saturating_sub(inner.last_crash_time) > CRASH_WINDOW_MS {
            inner.crash_count = 0;
        }
        inner.crash_count = inner.crash_count.saturating_add(1);
        inner.last_crash_time = now;
        inner.restart_in_progress = true;

        inner.crash_count
    };

    if attempt > MAX_CRASH_RESTARTS {
        let msg = format!(
            "Backend crashed {} times in {}s, giving up",
            attempt,
            CRASH_WINDOW_MS / 1000
        );
        error!("{msg}");
        let snapshot = state
            .publish_failed(app, "backend_restart_limit", &msg)
            .await;
        state.inner.lock().await.restart_in_progress = false;
        let _ = app.emit(
            "backend-crash",
            snapshot
                .detail
                .as_deref()
                .unwrap_or("Backend restart limit reached"),
        );
        return;
    }

    loop {
        state.publish_restarting(app, attempt).await;
        // Keep legacy events until all SSE/cache consumers use the status stream.
        let _ = app.emit("backend-restarting", ());

        // Exponential backoff: 1s, 2s, 4s (capped)
        let delay_ms = std::cmp::min(1000 * 2u64.pow(attempt - 1), 4000);
        info!(
            "Backend crashed (attempt {}/{}), restarting in {}ms...",
            attempt, MAX_CRASH_RESTARTS, delay_ms
        );
        sleep(Duration::from_millis(delay_ms)).await;

        // Check if intentional stop was requested during the delay.
        if state.inner.lock().await.intentional_stop {
            return;
        }

        match state.start(app).await {
            Ok(url) => {
                info!("Backend restarted successfully at {url}");
                state.inner.lock().await.restart_in_progress = false;
                // Notify legacy consumers after Ready has been published.
                let _ = app.emit("backend-restart", &url);
                return;
            }
            Err(err) => {
                error!("Backend restart attempt {attempt} failed: {err}");

                let mut inner = state.inner.lock().await;
                if inner.intentional_stop {
                    return;
                }
                inner.crash_count = inner.crash_count.saturating_add(1);
                inner.last_crash_time = epoch_ms();
                attempt = inner.crash_count;
                drop(inner);

                if attempt > MAX_CRASH_RESTARTS {
                    let msg = format!(
                        "Backend restart failed after {MAX_CRASH_RESTARTS} attempts: {err}"
                    );
                    let snapshot = state
                        .publish_failed(app, "backend_restart_failed", &msg)
                        .await;
                    state.inner.lock().await.restart_in_progress = false;
                    let _ = app.emit(
                        "backend-crash",
                        snapshot
                            .detail
                            .as_deref()
                            .unwrap_or("Backend restart failed"),
                    );
                    return;
                }
            }
        }
    }
}

fn validate_backend_resources(
    backend_dir: &Path,
    backend_path: &Path,
    desktop_log_path: &Path,
) -> Result<(), String> {
    if !backend_dir.exists() {
        let msg = format!(
            "Packaged backend directory is missing: {}",
            backend_dir.display()
        );
        write_desktop_log(desktop_log_path, &msg);
        return Err(msg);
    }

    if !backend_path.exists() {
        let msg = format!(
            "Packaged backend executable is missing: {}",
            backend_path.display()
        );
        write_desktop_log(desktop_log_path, &msg);
        return Err(msg);
    }

    #[cfg(target_os = "windows")]
    {
        let python_dll = backend_dir.join("_internal").join("python312.dll");
        if !python_dll.exists() {
            let msg = format!(
                "Packaged Python runtime is missing: {}",
                python_dll.display()
            );
            write_desktop_log(desktop_log_path, &msg);
            return Err(msg);
        }
    }

    // Node.js runtime (optional — used by OpenClaw channels)
    let nodejs_dir = backend_dir.parent().unwrap_or(backend_dir).join("nodejs");
    let node_binary = if cfg!(target_os = "windows") {
        nodejs_dir.join("node.exe")
    } else {
        nodejs_dir.join("bin").join("node")
    };
    if nodejs_dir.exists() && !node_binary.exists() {
        write_desktop_log(
            desktop_log_path,
            &format!(
                "WARNING: nodejs directory exists but node binary missing: {}",
                node_binary.display()
            ),
        );
    }

    write_desktop_log(
        desktop_log_path,
        &format!(
            "Validated packaged backend resources | backend_dir={} | backend_path={} | nodejs={}",
            backend_dir.display(),
            backend_path.display(),
            node_binary.exists()
        ),
    );
    Ok(())
}

fn write_desktop_log(log_path: &Path, message: &str) {
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "{message}");
    }
}

/// Poll the backend's session token file until it appears or we time out.
///
/// The backend writes `session_token.json` at startup with mode 0600. We
/// read it directly rather than trusting any HTTP field, so a tampered
/// backend on the same host — or a malicious process masquerading on the
/// same port — cannot inject an attacker-chosen token: the reader's own
/// UID is what gates access via filesystem permissions.
async fn load_session_token(data_dir: &Path) -> Result<String, String> {
    let token_path = data_dir.join("session_token.json");
    let deadline = std::time::Instant::now() + TOKEN_MAX_WAIT;

    loop {
        match tokio::fs::read_to_string(&token_path).await {
            Ok(raw) => {
                // Minimal JSON parse: we expect `{"token": "suxiaoyou_st_..."}`.
                // Bringing in serde_json here would be overkill for a two-field
                // file we control the format of, so we extract the string
                // value directly.
                if let Some(token) = extract_token_field(&raw) {
                    return Ok(token);
                }
                return Err(format!(
                    "session_token.json at {} is malformed",
                    token_path.display()
                ));
            }
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                if std::time::Instant::now() >= deadline {
                    return Err(format!(
                        "Timed out waiting for {} (backend did not write session token)",
                        token_path.display()
                    ));
                }
                sleep(TOKEN_POLL_INTERVAL).await;
            }
            Err(err) => {
                return Err(format!("Cannot read {}: {}", token_path.display(), err));
            }
        }
    }
}

/// Extract the `"token"` field from `{"token": "..."}` without pulling in
/// a full JSON parser. The file is written by our own backend so the
/// format is fixed; we only need to tolerate whitespace variations.
fn extract_token_field(raw: &str) -> Option<String> {
    let key = "\"token\"";
    let start = raw.find(key)?;
    // Advance past the key, any whitespace, and the colon.
    let after_key = raw[start + key.len()..].trim_start();
    let after_colon = after_key.strip_prefix(':')?.trim_start();
    let rest = after_colon.strip_prefix('"')?;
    let end = rest.find('"')?;
    let value = &rest[..end];
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

fn read_recent_log_lines(path: &Path, max_lines: usize) -> String {
    match std::fs::read_to_string(path) {
        Ok(content) => {
            let lines: Vec<&str> = content.lines().rev().take(max_lines).collect();
            if lines.is_empty() {
                "<backend log empty>".to_string()
            } else {
                lines.into_iter().rev().collect::<Vec<_>>().join("\n")
            }
        }
        Err(_) => "<backend log unavailable>".to_string(),
    }
}

/// Kill any Unix descendants that still belong to the backend's isolated PGID.
/// This is also called after the parent has already been reaped, because a
/// crashed parent does not imply that helpers in its process group exited.
fn terminate_residual_process_group(pid: Option<u32>) -> Result<(), String> {
    #[cfg(unix)]
    if let Some(pid) = pid {
        let pgid = i32::try_from(pid)
            .map_err(|_| format!("Backend pid {pid} cannot be represented as a process group"))?;
        // SAFETY: production and test children are spawned with
        // process_group(0), making their PID the isolated PGID.
        let result = unsafe { libc::kill(-pgid, libc::SIGKILL) };
        if result == 0 {
            info!("Sent SIGKILL to backend process group {pgid}");
        } else {
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() != Some(libc::ESRCH) {
                return Err(format!(
                    "failed to signal backend process group {pgid}: {err}"
                ));
            }
        }
    }

    #[cfg(not(unix))]
    let _ = pid;

    Ok(())
}

/// Kill a process and its entire tree, then confirm the direct child is reaped.
async fn kill_process_tree(child: &mut Child) -> Result<(), String> {
    // Capture before try_wait/wait: Tokio clears Child::id() after reaping, but
    // the PID remains the Unix process-group identity for surviving descendants.
    let pid = child.id();
    match child.try_wait() {
        Ok(Some(status)) => {
            terminate_residual_process_group(pid)?;
            info!("Backend parent was already reaped with status {status}");
            return Ok(());
        }
        Ok(None) => {}
        Err(err) => warn!("Initial backend process inspection failed: {err}"),
    }

    let mut tree_error: Option<String> = None;

    #[cfg(unix)]
    if let Err(err) = terminate_residual_process_group(pid) {
        tree_error = Some(err);
    }

    #[cfg(target_os = "windows")]
    if let Some(pid) = pid {
        let mut taskkill = tokio::process::Command::new("taskkill");
        taskkill
            .kill_on_drop(true)
            .args(["/PID", &pid.to_string(), "/T", "/F"]);
        match tokio::time::timeout(TASKKILL_TIMEOUT, taskkill.output()).await {
            Ok(Ok(output)) if output.status.success() => {
                info!("taskkill succeeded for pid {pid}");
            }
            Ok(Ok(output)) => {
                let stderr = String::from_utf8_lossy(&output.stderr);
                tree_error = Some(format!(
                    "taskkill failed for pid {pid} (status {}): {}",
                    output.status,
                    sanitize_status_detail(stderr.trim(), None)
                ));
            }
            Ok(Err(err)) => {
                tree_error = Some(format!("failed to run taskkill for pid {pid}: {err}"));
            }
            Err(_) => {
                tree_error = Some(format!(
                    "taskkill timed out for pid {pid} after {}ms",
                    TASKKILL_TIMEOUT.as_millis()
                ));
            }
        }
    }

    // Always request direct parent termination as a fallback. `start_kill` is
    // synchronous, so the bounded wait below is the only await in this phase.
    let direct_kill_error = child.start_kill().err().map(|err| err.to_string());
    let status = match tokio::time::timeout(PROCESS_KILL_TIMEOUT, child.wait()).await {
        Ok(Ok(status)) => status,
        Ok(Err(wait_error)) => {
            return Err(format!(
                "failed to reap backend parent{}: {wait_error}",
                direct_kill_error
                    .as_deref()
                    .map(|err| format!(" (direct kill also failed: {err})"))
                    .unwrap_or_default()
            ));
        }
        Err(_) => {
            return Err(format!(
                "timed out after {}ms while terminating backend parent{}",
                PROCESS_KILL_TIMEOUT.as_millis(),
                direct_kill_error
                    .as_deref()
                    .map(|err| format!(" (direct kill failed: {err})"))
                    .unwrap_or_default()
            ));
        }
    };

    if let Some(tree_error) = tree_error {
        return Err(format!(
            "{tree_error}; direct parent exited with status {status}, but descendant cleanup could not be confirmed"
        ));
    }

    info!("Backend process tree terminated; parent status: {status}");
    Ok(())
}

#[cfg(test)]
mod startup_contract_tests {
    use super::{
        bundled_node_bin_dir, clear_watchdog_generation, kill_process_tree, next_backend_status,
        predecessor_data_dir, prepend_runtime_path, take_process_for_generation, BackendPhase,
        BackendState, StatusUpdate, HEALTH_STARTUP_TIMEOUT, MAX_STATUS_DETAIL_CHARS,
    };
    use serde_json::json;
    use std::path::Path;
    use std::process::Stdio;
    use std::time::Duration;
    #[cfg(unix)]
    use tokio::io::{AsyncBufReadExt, BufReader};
    use tokio::process::{Child, Command};

    #[test]
    fn production_cold_start_health_budget_is_at_least_ninety_seconds() {
        assert!(
            HEALTH_STARTUP_TIMEOUT >= Duration::from_secs(90),
            "production backend cold-start budget must be at least 90s, got {HEALTH_STARTUP_TIMEOUT:?}"
        );
    }

    #[test]
    fn predecessor_data_directory_is_a_sibling_of_current_app_data() {
        let current = Path::new("/application-support/com.chaoyuanxinzhi.suxiaoyou");
        let expected_identifier = format!("com.{}.desktop", concat!("open", "yak"));
        assert_eq!(
            predecessor_data_dir(current).unwrap(),
            Path::new("/application-support")
                .join(expected_identifier)
                .join("data")
        );
    }

    #[test]
    fn bundled_node_directory_is_first_in_backend_path_without_duplicates() {
        let resource_dir = Path::new("/application/resources");
        let node_bin = bundled_node_bin_dir(resource_dir);
        let current = std::env::join_paths([
            Path::new("/usr/bin"),
            node_bin.as_path(),
            Path::new("/custom/bin"),
        ])
        .unwrap();

        let combined = prepend_runtime_path(&node_bin, Some(current.as_os_str())).unwrap();
        let entries: Vec<_> = std::env::split_paths(&combined).collect();

        assert_eq!(entries.first(), Some(&node_bin));
        assert_eq!(entries.iter().filter(|path| *path == &node_bin).count(), 1);
        assert!(entries.iter().any(|path| path == Path::new("/usr/bin")));
        assert!(entries.iter().any(|path| path == Path::new("/custom/bin")));
    }

    #[test]
    fn restarting_snapshot_serializes_exact_frontend_contract() {
        let status = next_backend_status(
            6,
            StatusUpdate::restarting(Some("http://127.0.0.1:43123".into()), 2),
            None,
        );

        assert_eq!(
            serde_json::to_value(status).unwrap(),
            json!({
                "revision": 7,
                "phase": "restarting",
                "url": "http://127.0.0.1:43123",
                "attempt": 2,
                "max_attempts": 3,
            })
        );
    }

    #[test]
    fn every_phase_uses_lowercase_wire_value() {
        let expected = [
            (BackendPhase::Initializing, "initializing"),
            (BackendPhase::Ready, "ready"),
            (BackendPhase::Restarting, "restarting"),
            (BackendPhase::Failed, "failed"),
        ];

        for (phase, wire_value) in expected {
            assert_eq!(serde_json::to_value(phase).unwrap(), json!(wire_value));
        }
    }

    #[test]
    fn transitions_increment_revision_and_clear_phase_specific_fields() {
        let ready = next_backend_status(
            0,
            StatusUpdate::ready("http://127.0.0.1:40100".into()),
            None,
        );
        let restarting = next_backend_status(
            ready.revision,
            StatusUpdate::restarting(ready.url.clone(), 1),
            None,
        );
        let failed = next_backend_status(
            restarting.revision,
            StatusUpdate::failed("backend_restart_failed", "could not restart"),
            None,
        );

        assert_eq!(
            (ready.revision, restarting.revision, failed.revision),
            (1, 2, 3)
        );
        assert_eq!(failed.phase, BackendPhase::Failed);
        assert!(failed.url.is_none());
        assert!(failed.attempt.is_none());
        assert!(failed.max_attempts.is_none());
    }

    #[test]
    fn failed_snapshot_redacts_tokens_bounds_detail_and_omits_null_fields() {
        let prefixed_secret = "suxiaoyou_st_top-secret_value";
        let known_secret = "opaque-known-session-secret";
        let raw_detail = format!(
            "auth failed for {prefixed_secret} and {known_secret}: {}",
            "故".repeat(MAX_STATUS_DETAIL_CHARS + 50)
        );
        let status = next_backend_status(
            9,
            StatusUpdate::failed("backend_start_failed", &raw_detail),
            Some(known_secret),
        );
        let serialized = serde_json::to_string(&status).unwrap();
        let value = serde_json::to_value(&status).unwrap();
        let detail = status.detail.as_deref().unwrap();

        assert!(!serialized.contains(prefixed_secret));
        assert!(!serialized.contains(known_secret));
        assert!(serialized.contains("[redacted]"));
        assert!(detail.chars().count() <= MAX_STATUS_DETAIL_CHARS);
        assert!(value.get("session_token").is_none());
        assert!(value.get("url").is_none());
        assert!(value.get("attempt").is_none());
        assert!(value.get("max_attempts").is_none());
    }

    #[cfg(unix)]
    fn spawn_exiting_child() -> Child {
        let mut command = Command::new("sh");
        command
            .kill_on_drop(true)
            .process_group(0)
            .args(["-c", "exit 23"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command.spawn().expect("spawn exiting test child")
    }

    #[cfg(windows)]
    fn spawn_exiting_child() -> Child {
        let mut command = Command::new("cmd");
        command
            .kill_on_drop(true)
            .args(["/C", "exit", "23"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command.spawn().expect("spawn exiting test child")
    }

    #[cfg(any(unix, windows))]
    #[tokio::test]
    async fn startup_probe_reaps_an_exited_child_without_waiting_for_health_timeout() {
        let state = BackendState::new();
        {
            let mut inner = state.inner.lock().await;
            inner.generation = 1;
            inner.process = Some(spawn_exiting_child());
        }

        let diagnostic = tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(diagnostic) = state.startup_process_error(1).await {
                    break diagnostic;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("exited child should be detected immediately");

        assert!(diagnostic.contains("exited before becoming ready"));
        assert!(diagnostic.contains("23"));
        assert!(state.inner.lock().await.process.is_none());
    }

    #[cfg(unix)]
    fn spawn_sleeping_child() -> Child {
        let mut command = Command::new("sh");
        command
            .kill_on_drop(true)
            .process_group(0)
            .args(["-c", "exec sleep 30"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command.spawn().expect("spawn sleeping test child")
    }

    #[cfg(unix)]
    async fn process_is_effectively_running(pid: u32) -> bool {
        let output = Command::new("ps")
            .args(["-o", "state=", "-p", &pid.to_string()])
            .output()
            .await;
        match output {
            Ok(output) if output.status.success() => {
                let state = String::from_utf8_lossy(&output.stdout);
                let state = state.trim();
                !state.is_empty() && !state.starts_with('Z')
            }
            _ => false,
        }
    }

    #[cfg(unix)]
    async fn wait_until_not_running(pid: u32) -> bool {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
        while process_is_effectively_running(pid).await {
            if tokio::time::Instant::now() >= deadline {
                return false;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        true
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stale_generation_cannot_take_new_child_or_clear_new_watchdog() {
        let state = BackendState::new();
        let child = spawn_sleeping_child();
        let pid = child.id().expect("sleeping child pid");

        let stale_take = {
            let mut inner = state.inner.lock().await;
            inner.generation = 2;
            inner.watchdog_generation = Some(2);
            inner.session_token = Some("suxiaoyou_st_generation_two".into());
            inner.process = Some(child);

            let stale_take = take_process_for_generation(&mut inner, 1);
            clear_watchdog_generation(&mut inner, 1);
            assert_eq!(inner.watchdog_generation, Some(2));
            assert_eq!(inner.process.as_ref().and_then(Child::id), Some(pid));
            assert_eq!(
                inner.session_token.as_deref(),
                Some("suxiaoyou_st_generation_two")
            );
            stale_take
        };

        assert!(stale_take.is_none());
        state
            .cleanup_failed_start(2)
            .await
            .expect("clean current generation");
        assert!(wait_until_not_running(pid).await);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn process_group_cleanup_terminates_parent_and_descendant() {
        let mut command = Command::new("sh");
        command
            .kill_on_drop(true)
            .process_group(0)
            .args(["-c", "sleep 30 & echo $!; wait"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut parent = command.spawn().expect("spawn process-group parent");
        let parent_pid = parent.id().expect("parent pid");
        let stdout = parent.stdout.take().expect("parent stdout");
        let mut lines = BufReader::new(stdout).lines();
        let descendant_line =
            match tokio::time::timeout(Duration::from_secs(2), lines.next_line()).await {
                Ok(Ok(Some(line))) => line,
                other => {
                    let _ = kill_process_tree(&mut parent).await;
                    panic!("failed to read descendant pid: {other:?}");
                }
            };
        drop(lines);
        let descendant_pid: u32 = descendant_line
            .trim()
            .parse()
            .expect("numeric descendant pid");

        // SAFETY: both PIDs came from live children spawned by this test.
        assert_eq!(
            unsafe { libc::getpgid(parent_pid as i32) },
            parent_pid as i32
        );
        // SAFETY: see above. The descendant must inherit the isolated PGID.
        assert_eq!(
            unsafe { libc::getpgid(descendant_pid as i32) },
            parent_pid as i32
        );

        kill_process_tree(&mut parent)
            .await
            .expect("terminate process group");
        assert!(wait_until_not_running(parent_pid).await);
        assert!(wait_until_not_running(descendant_pid).await);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn exited_parent_probe_still_terminates_surviving_descendant_group() {
        let mut command = Command::new("sh");
        command
            .kill_on_drop(true)
            .process_group(0)
            .args(["-c", "sleep 30 & echo $!; exit 23"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut parent = command.spawn().expect("spawn exiting process-group parent");
        let stdout = parent.stdout.take().expect("parent stdout");
        let descendant_line = tokio::time::timeout(
            Duration::from_secs(2),
            BufReader::new(stdout).lines().next_line(),
        )
        .await
        .expect("descendant pid timeout")
        .expect("read descendant pid")
        .expect("descendant pid line");
        let descendant_pid: u32 = descendant_line
            .trim()
            .parse()
            .expect("numeric descendant pid");

        let state = BackendState::new();
        {
            let mut inner = state.inner.lock().await;
            inner.generation = 12;
            inner.process = Some(parent);
        }

        let diagnostic = tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(diagnostic) = state.startup_process_error(12).await {
                    break diagnostic;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("exited parent should be observed");

        assert!(diagnostic.contains("23"));
        assert!(wait_until_not_running(descendant_pid).await);
        assert!(state.inner.lock().await.process.is_none());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn failed_start_cleanup_and_stop_are_idempotent() {
        let state = BackendState::new();
        let child = spawn_sleeping_child();
        let pid = child.id().expect("sleeping child pid");
        {
            let mut inner = state.inner.lock().await;
            inner.generation = 9;
            inner.session_token = Some("suxiaoyou_st_cleanup_test".into());
            inner.process = Some(child);
        }

        state
            .cleanup_failed_start(9)
            .await
            .expect("first cleanup succeeds");
        state
            .cleanup_failed_start(9)
            .await
            .expect("second cleanup is a no-op");
        state.stop().await.expect("first stop succeeds");
        state.stop().await.expect("second stop is a no-op");

        assert!(wait_until_not_running(pid).await);
        let inner = state.inner.lock().await;
        assert!(inner.process.is_none());
        assert!(inner.session_token.is_none());
        assert!(inner.intentional_stop);
    }
}
