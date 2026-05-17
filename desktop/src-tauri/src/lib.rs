//! Cato Desktop — Tauri backend (v0.2.1 — activity indicator)
//!
//! Manages the Python daemon sidecar, system tray, global hotkey,
//! and native notifications.

use serde::Serialize;
use std::sync::Arc;
use tauri::{
    AppHandle, Emitter, Manager, RunEvent, WindowEvent,
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
};
use tokio::sync::Mutex;

mod sidecar;

/// Shared application state
pub struct AppState {
    pub sidecar: Arc<Mutex<sidecar::SidecarManager>>,
}

/// Health status returned to the frontend
#[derive(Clone, Serialize)]
struct DaemonStatus {
    running: bool,
    http_port: u16,
    ws_port: u16,
    daemon_token: Option<String>,
}

/// Tauri command: get daemon status
#[tauri::command]
async fn get_daemon_status(state: tauri::State<'_, AppState>) -> Result<DaemonStatus, String> {
    let mut mgr = state.sidecar.lock().await;
    Ok(DaemonStatus {
        running: mgr.is_running().await,
        http_port: mgr.http_port(),
        ws_port: mgr.ws_port(),
        daemon_token: sidecar::SidecarManager::daemon_token(),
    })
}

/// Tauri command: restart the daemon
#[tauri::command]
async fn restart_daemon(state: tauri::State<'_, AppState>) -> Result<(), String> {
    let mut mgr = state.sidecar.lock().await;
    mgr.stop().await;
    mgr.start().await.map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // ── Plugins ──
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        // ── State ──
        .manage(AppState {
            // The desktop uses the daemon's aiohttp surface for both HTTP and WebSocket traffic.
            sidecar: Arc::new(Mutex::new(sidecar::SidecarManager::new(8080, 8080))),
        })
        // ── Commands ──
        .invoke_handler(tauri::generate_handler![
            get_daemon_status,
            restart_daemon,
        ])
        // ── Setup ──
        .setup(|app| {
            let handle = app.handle().clone();

            // ── System Tray ──
            setup_tray(&handle)?;

            // ── Global Shortcut ──
            setup_global_shortcut(&handle);

            // ── Start sidecar with crash monitoring ──
            let sidecar = handle.state::<AppState>().sidecar.clone();
            let emit_handle = handle.clone();
            tauri::async_runtime::spawn(async move {
                let mut mgr = sidecar.lock().await;
                if let Err(e) = mgr.start().await {
                    log::error!("Failed to start Cato daemon: {}", e);
                    let _ = emit_handle.emit("daemon-error", e.to_string());
                }
            });

            Ok(())
        })
        // ── Run ──
        .build(tauri::generate_context!())
        .expect("error building tauri application")
        .run(|app_handle, event| {
            match event {
                RunEvent::WindowEvent {
                    event: WindowEvent::CloseRequested { api, .. },
                    label,
                    ..
                } => {
                    if label == "main" {
                        // Minimize to tray instead of quitting
                        api.prevent_close();
                        if let Some(window) = app_handle.get_webview_window("main") {
                            let _ = window.hide();
                        }
                    }
                }
                RunEvent::ExitRequested { .. } => {
                    // Cleanup sidecar on exit — use spawn to avoid deadlock
                    let sidecar = app_handle.state::<AppState>().sidecar.clone();
                    tauri::async_runtime::spawn(async move {
                        let mut mgr = sidecar.lock().await;
                        mgr.stop().await;
                    });
                }
                _ => {}
            }
        });
}

/// Set up the system tray with a menu
fn setup_tray(handle: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let open = MenuItem::with_id(handle, "open", "Open Cato", true, None::<&str>)?;
    let restart = MenuItem::with_id(handle, "restart", "Restart Daemon", true, None::<&str>)?;
    let quit = MenuItem::with_id(handle, "quit", "Quit", true, None::<&str>)?;

    let menu = Menu::with_items(handle, &[&open, &restart, &quit])?;

    let handle_clone = handle.clone();
    TrayIconBuilder::new()
        .tooltip("Cato AI")
        .menu(&menu)
        .on_menu_event(move |app, event| {
            match event.id.as_ref() {
                "open" => {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.set_focus();
                    }
                }
                "restart" => {
                    let sidecar = app.state::<AppState>().sidecar.clone();
                    tauri::async_runtime::spawn(async move {
                        let mut mgr = sidecar.lock().await;
                        mgr.stop().await;
                        if let Err(e) = mgr.start().await {
                            log::error!("Failed to restart daemon: {}", e);
                        }
                    });
                }
                "quit" => {
                    // Spawn cleanup then exit — avoids block_on deadlock
                    let sidecar = app.state::<AppState>().sidecar.clone();
                    let app_handle = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let mut mgr = sidecar.lock().await;
                        mgr.stop().await;
                        drop(mgr); // Release lock before exit
                        app_handle.exit(0);
                    });
                }
                _ => {}
            }
        })
        .build(&handle_clone)?;

    Ok(())
}

/// Register global shortcut to toggle the main window
fn setup_global_shortcut(handle: &AppHandle) {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;

    let handle_clone = handle.clone();
    let _ = handle.global_shortcut().on_shortcut("CmdOrCtrl+Shift+C", move |_app, _shortcut, _event| {
        if let Some(window) = handle_clone.get_webview_window("main") {
            if window.is_visible().unwrap_or(false) {
                let _ = window.hide();
            } else {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
    });
}
