//! Sidecar management for the Cato Python daemon.
//!
//! Spawns `python -m cato start --channel webchat` as a child process and monitors its health.
//! Override the interpreter with `CATO_PYTHON` (defaults to `python` on Windows, `python3` elsewhere).
//! Gracefully shuts down on app exit.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, Command};
use tokio::time::{sleep, Duration};

#[cfg(windows)]
const DEFAULT_PYTHON: &str = "python";
#[cfg(not(windows))]
const DEFAULT_PYTHON: &str = "python3";

/// Manages the Cato daemon sidecar process.
pub struct SidecarManager {
    child: Option<Child>,
    http_port: u16,
    ws_port: u16,
}

impl SidecarManager {
    pub fn new(http_port: u16, ws_port: u16) -> Self {
        Self {
            child: None,
            http_port,
            ws_port,
        }
    }

    pub fn http_port(&self) -> u16 {
        self.http_port
    }

    pub fn ws_port(&self) -> u16 {
        self.ws_port
    }

    pub fn daemon_token() -> Option<String> {
        let token_path = Self::cato_data_dir()?.join("daemon.token");
        std::fs::read_to_string(token_path)
            .ok()
            .map(|token| token.trim().to_string())
            .filter(|token| !token.is_empty())
    }

    /// Check if the daemon is running — either as a child process we spawned,
    /// or as an externally-started daemon already listening on the HTTP port.
    ///
    /// This handles the case where the user started `cato start` manually before
    /// opening the desktop app: the child is None, but the daemon health route
    /// is still responding on the discovered HTTP port.
    pub async fn is_running(&mut self) -> bool {
        self.refresh_ports_from_disk();

        // 1. If we spawned a child, check it first
        if let Some(ref mut child) = self.child {
            match child.try_wait() {
                Ok(Some(_status)) => {
                    // Process has exited — clean up and fall through to HTTP check
                    self.child = None;
                }
                Ok(None) => return self.check_http_health().await,
                Err(_) => {
                    self.child = None;
                }
            }
        }

        self.refresh_ports_from_disk();

        // 2. No child (never started, or it exited) — check if daemon is
        //    reachable on the HTTP port (external process or race condition).
        self.check_http_health().await
    }

    /// Return true if the daemon health endpoint responds with HTTP 200.
    async fn check_http_health(&self) -> bool {
        let url = format!("http://127.0.0.1:{}/health", self.http_port);
        let client = reqwest::Client::new();
        matches!(
            client.get(&url).timeout(std::time::Duration::from_millis(800)).send().await,
            Ok(resp) if resp.status().is_success()
        )
    }

    /// Start the Cato daemon as a child process (`python -m cato start --channel webchat`).
    pub async fn start(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        // Check if already running (handle crashed state)
        if self.is_running().await {
            return Ok(());
        }

        let python_exe = Self::python_executable();
        let sidecar_env = Self::load_env_file();

        // Clear any stale PID file by running `python -m cato stop` first (ignores errors)
        log::info!("Clearing any stale Cato daemon state...");
        let _ = Command::new(&python_exe)
            .args(["-m", "cato", "stop"])
            .output()
            .await;
        sleep(Duration::from_millis(500)).await;

        log::info!(
            "Starting Cato daemon: {} -m cato start --channel webchat",
            python_exe.display()
        );

        let mut cmd = Command::new(&python_exe);
        cmd.args(["-m", "cato", "start", "--channel", "webchat"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);

        for (key, value) in &sidecar_env {
            if std::env::var_os(key).is_none() {
                cmd.env(key, value);
            }
        }

        let mut child = cmd.spawn().map_err(|e| {
            format!(
                "Failed to spawn Cato daemon ({} -m cato …): {}",
                python_exe.display(),
                e
            )
        })?;

        if let Some(stdout) = child.stdout.take() {
            Self::spawn_log_drain(stdout, false);
        }
        if let Some(stderr) = child.stderr.take() {
            Self::spawn_log_drain(stderr, true);
        }

        self.child = Some(child);

        // Wait for the daemon to become healthy. Cold Python starts can take
        // longer on Windows when optional ML/MCP modules are imported.
        self.wait_for_health(120).await?;

        log::info!("Cato daemon is healthy on port {}", self.http_port);
        Ok(())
    }

    /// Stop the Cato daemon gracefully.
    pub async fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            log::info!("Stopping Cato daemon...");

            // Try graceful shutdown via `python -m cato stop`
            let python_exe = Self::python_executable();
            let _ = Command::new(&python_exe)
                .args(["-m", "cato", "stop"])
                .output()
                .await;

            // Wait up to 5 seconds for the process to exit
            let timeout = sleep(Duration::from_secs(5));
            tokio::pin!(timeout);

            tokio::select! {
                _ = child.wait() => {
                    log::info!("Cato daemon stopped gracefully");
                }
                _ = &mut timeout => {
                    log::warn!("Cato daemon did not stop in time, killing...");
                    let _ = child.kill().await;
                }
            }
        }
    }

    /// Poll the health endpoint until the daemon is ready.
    async fn wait_for_health(
        &mut self,
        timeout_secs: u64,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let client = reqwest::Client::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(timeout_secs);

        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err("Cato daemon health check timed out".into());
            }

            self.refresh_ports_from_disk();
            let url = format!("http://127.0.0.1:{}/health", self.http_port);
            match client.get(&url).timeout(Duration::from_secs(2)).send().await {
                Ok(resp) if resp.status().is_success() => return Ok(()),
                _ => {}
            }

            sleep(Duration::from_millis(500)).await;
        }
    }

    fn refresh_ports_from_disk(&mut self) {
        let Some(port_path) = Self::port_file_path() else {
            return;
        };

        let Ok(raw_port) = std::fs::read_to_string(&port_path) else {
            return;
        };

        let Ok(http_port) = raw_port.trim().parse::<u16>() else {
            log::warn!("Invalid port file contents in {}", port_path.display());
            return;
        };

        self.http_port = http_port;
        // Desktop chat and coding-agent traffic both ride the aiohttp /ws surface.
        self.ws_port = http_port;
    }

    fn spawn_log_drain<R>(reader: R, is_stderr: bool)
    where
        R: AsyncRead + Unpin + Send + 'static,
    {
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                if is_stderr {
                    log::warn!("[cato] {}", line);
                } else {
                    log::info!("[cato] {}", line);
                }
            }
        });
    }

    /// Load supplemental environment variables from the standard Cato .env locations.
    /// Existing process env vars always win over values from disk.
    fn load_env_file() -> BTreeMap<String, String> {
        for env_path in Self::env_file_candidates() {
            if !env_path.exists() {
                continue;
            }

            match std::fs::read_to_string(&env_path) {
                Ok(contents) => {
                    let parsed = Self::parse_dotenv(&contents);
                    if !parsed.is_empty() {
                        log::info!("Loaded sidecar environment from {}", env_path.display());
                        return parsed;
                    }
                }
                Err(err) => {
                    log::warn!("Failed to read {}: {}", env_path.display(), err);
                }
            }
        }

        BTreeMap::new()
    }

    fn env_file_candidates() -> Vec<PathBuf> {
        let mut candidates = Vec::new();

        if let Ok(path) = std::env::var("CATO_ENV_FILE") {
            let path = PathBuf::from(path);
            if path.is_absolute() {
                candidates.push(path);
            } else if let Ok(cwd) = std::env::current_dir() {
                candidates.push(cwd.join(path));
            }
        }

        if let Some(data_dir) = Self::cato_data_dir() {
            candidates.push(data_dir.join(".env"));
        }

        if let Some(base_dir) = Self::current_exe_base_dir() {
            candidates.push(base_dir.join(".env"));
        }

        if let Ok(cwd) = std::env::current_dir() {
            candidates.push(cwd.join(".env"));
        }

        candidates
    }

    fn parse_dotenv(contents: &str) -> BTreeMap<String, String> {
        let mut out = BTreeMap::new();

        for raw_line in contents.lines() {
            let line = raw_line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }

            let line = line.strip_prefix("export ").unwrap_or(line);
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };

            let key = key.trim();
            if key.is_empty() {
                continue;
            }

            let value = value.trim();
            let value = if value.len() >= 2
                && ((value.starts_with('"') && value.ends_with('"'))
                    || (value.starts_with('\'') && value.ends_with('\'')))
            {
                value[1..value.len() - 1].to_string()
            } else {
                value.to_string()
            };

            out.insert(key.to_string(), value);
        }

        out
    }

    fn cato_data_dir() -> Option<PathBuf> {
        if cfg!(windows) {
            dirs::config_dir().map(|dir| dir.join("cato"))
        } else {
            dirs::home_dir().map(|dir| dir.join(".cato"))
        }
    }

    fn port_file_path() -> Option<PathBuf> {
        Self::cato_data_dir().map(|dir| dir.join("cato.port"))
    }

    fn current_exe_base_dir() -> Option<PathBuf> {
        let exe = std::env::current_exe().ok()?;
        let exe_dir = exe.parent()?;
        if exe_dir.ends_with("deps") {
            Some(exe_dir.parent().unwrap_or(exe_dir).to_path_buf())
        } else {
            Some(exe_dir.to_path_buf())
        }
    }

    /// Python interpreter for `python -m cato …`. Set `CATO_PYTHON` to an absolute path if needed.
    fn python_executable() -> PathBuf {
        if let Ok(path) = std::env::var("CATO_PYTHON") {
            let p = PathBuf::from(path.trim());
            if !p.as_os_str().is_empty() {
                log::info!("Using Python from CATO_PYTHON: {}", p.display());
                return p;
            }
        }
        PathBuf::from(DEFAULT_PYTHON)
    }
}

impl Drop for SidecarManager {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _: Result<(), std::io::Error> = child.start_kill();
        }
    }
}
