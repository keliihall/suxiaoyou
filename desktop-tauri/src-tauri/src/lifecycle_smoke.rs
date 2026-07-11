//! Opt-in control channel for packaged desktop lifecycle smoke tests.
//!
//! The channel is dormant unless the launcher supplies an absolute
//! `SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR`. Release CI uses the directory to
//! observe backend readiness and request the same graceful `app.exit(0)` path
//! used by the native Quit commands. No bearer token or user content is
//! written to the channel.

use serde::Serialize;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tauri::{AppHandle, Manager};

const CONTROL_DIRECTORY_ENV: &str = "SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR";
const EXIT_REQUEST_FILE: &str = "request-exit";
const MAX_FAILURE_DETAIL_CHARS: usize = 2_000;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ReadyMarker {
    desktop_pid: u32,
    backend_pid: u32,
    backend_url: String,
    app_data_dir: String,
    app_log_dir: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CleanupMarker {
    desktop_pid: u32,
    backend_cleanup_ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    detail: Option<String>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct FailureMarker {
    desktop_pid: u32,
    detail: String,
}

pub fn publish_ready(
    app: &AppHandle,
    backend_url: &str,
    backend_pid: Option<u32>,
) -> Result<Option<PathBuf>, String> {
    let Some(directory) = configured_control_directory()? else {
        return Ok(None);
    };
    let backend_pid = backend_pid.ok_or("backend PID is unavailable after readiness")?;
    let app_data_dir = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot resolve lifecycle-smoke app data directory: {error}"))?;
    let app_log_dir = app
        .path()
        .app_log_dir()
        .map_err(|error| format!("cannot resolve lifecycle-smoke app log directory: {error}"))?;
    write_json_marker(
        &directory,
        "ready.json",
        &ReadyMarker {
            desktop_pid: std::process::id(),
            backend_pid,
            backend_url: backend_url.to_string(),
            app_data_dir: app_data_dir.to_string_lossy().into_owned(),
            app_log_dir: app_log_dir.to_string_lossy().into_owned(),
        },
    )?;
    Ok(Some(directory))
}

pub fn publish_start_failure(detail: &str) -> Result<(), String> {
    let Some(directory) = configured_control_directory()? else {
        return Ok(());
    };
    write_json_marker(
        &directory,
        "start-failed.json",
        &FailureMarker {
            desktop_pid: std::process::id(),
            detail: bounded_detail(detail),
        },
    )
}

pub fn publish_cleanup(ok: bool, detail: Option<&str>) -> Result<(), String> {
    let Some(directory) = configured_control_directory()? else {
        return Ok(());
    };
    write_json_marker(
        &directory,
        "cleanup.json",
        &CleanupMarker {
            desktop_pid: std::process::id(),
            backend_cleanup_ok: ok,
            detail: detail.map(bounded_detail),
        },
    )
}

pub fn arm_exit_request(app: AppHandle, directory: PathBuf) {
    tauri::async_runtime::spawn(async move {
        let request = directory.join(EXIT_REQUEST_FILE);
        loop {
            if request.is_file() {
                log::info!("Lifecycle smoke requested graceful desktop exit");
                app.exit(0);
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    });
}

fn configured_control_directory() -> Result<Option<PathBuf>, String> {
    control_directory_from(std::env::var_os(CONTROL_DIRECTORY_ENV))
}

fn control_directory_from(raw: Option<OsString>) -> Result<Option<PathBuf>, String> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    if raw.is_empty() {
        return Err(format!("{CONTROL_DIRECTORY_ENV} cannot be empty"));
    }
    let directory = PathBuf::from(raw);
    if !directory.is_absolute() {
        return Err(format!("{CONTROL_DIRECTORY_ENV} must be an absolute path"));
    }
    std::fs::create_dir_all(&directory).map_err(|error| {
        format!(
            "cannot create lifecycle-smoke directory {}: {error}",
            directory.display()
        )
    })?;
    Ok(Some(directory))
}

fn write_json_marker<T: Serialize>(directory: &Path, name: &str, value: &T) -> Result<(), String> {
    std::fs::create_dir_all(directory).map_err(|error| {
        format!(
            "cannot create lifecycle-smoke directory {}: {error}",
            directory.display()
        )
    })?;
    let destination = directory.join(name);
    let temporary = directory.join(format!(".{name}.{}.tmp", std::process::id()));
    let encoded = serde_json::to_vec_pretty(value)
        .map_err(|error| format!("cannot encode lifecycle-smoke marker {name}: {error}"))?;
    std::fs::write(&temporary, encoded).map_err(|error| {
        format!(
            "cannot write lifecycle-smoke marker {}: {error}",
            temporary.display()
        )
    })?;
    if destination.exists() {
        std::fs::remove_file(&destination).map_err(|error| {
            format!(
                "cannot replace lifecycle-smoke marker {}: {error}",
                destination.display()
            )
        })?;
    }
    std::fs::rename(&temporary, &destination).map_err(|error| {
        format!(
            "cannot publish lifecycle-smoke marker {}: {error}",
            destination.display()
        )
    })
}

fn bounded_detail(detail: &str) -> String {
    detail.chars().take(MAX_FAILURE_DETAIL_CHARS).collect()
}

#[cfg(test)]
mod tests {
    use super::{control_directory_from, write_json_marker};
    use serde_json::json;
    use std::ffi::OsString;

    #[test]
    fn control_channel_is_opt_in_and_requires_an_absolute_path() {
        assert_eq!(control_directory_from(None).unwrap(), None);
        assert!(control_directory_from(Some(OsString::from("relative"))).is_err());
    }

    #[test]
    fn marker_publish_is_atomic_and_valid_json() {
        let directory = std::env::temp_dir().join(format!(
            "suxiaoyou-lifecycle-smoke-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();

        write_json_marker(&directory, "ready.json", &json!({"ready": true})).unwrap();
        let parsed: serde_json::Value =
            serde_json::from_slice(&std::fs::read(directory.join("ready.json")).unwrap()).unwrap();
        assert_eq!(parsed, json!({"ready": true}));
        assert!(std::fs::read_dir(&directory).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".tmp")));

        std::fs::remove_dir_all(directory).unwrap();
    }
}
