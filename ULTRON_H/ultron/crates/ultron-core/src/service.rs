//! Windows Service integration.
//!
//! - `--install`   — registers the service with SCM (must be elevated)
//! - `--uninstall` — removes it
//! - `--service`   — runs as the service entry point (SCM invokes this)
//! - (default)     — runs as a foreground console app (dev)
//!
//! See `service/install.ps1` for the elevated install flow.

#![cfg(windows)]

use std::ffi::OsString;
use std::time::Duration;
use tracing::{error, info};
use windows_service::{
    define_windows_service,
    service::{
        ServiceAccess, ServiceControl, ServiceControlAccept, ServiceErrorControl,
        ServiceExitCode, ServiceInfo, ServiceStartType, ServiceState, ServiceStatus, ServiceType,
    },
    service_control_handler::{self, ServiceControlHandlerResult},
    service_dispatcher,
    service_manager::{ServiceManager, ServiceManagerAccess},
};

pub const SERVICE_NAME: &str = "UltronCore";
pub const SERVICE_DISPLAY: &str = "ULTRON Core Daemon";
pub const SERVICE_DESCRIPTION: &str = "Priyanshu's OS-level cognitive twin core daemon (ULTRON v5.1).";

define_windows_service!(ffi_service_main, service_main);

/// Entry point when SCM launches us. Hand off to the dispatcher.
pub fn run_as_service() -> windows_service::Result<()> {
    service_dispatcher::start(SERVICE_NAME, ffi_service_main)
}

/// Called by the dispatcher on the service thread.
fn service_main(_args: Vec<OsString>) {
    if let Err(e) = run_service() {
        error!("service_main error: {e:?}");
    }
}

fn run_service() -> windows_service::Result<()> {
    use std::sync::mpsc;
    let (stop_tx, stop_rx) = mpsc::channel::<()>();

    // Register a control handler. SCM will call this on stop / shutdown.
    let event_handler = move |control_event| -> ServiceControlHandlerResult {
        match control_event {
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            ServiceControl::Stop | ServiceControl::Shutdown => {
                let _ = stop_tx.send(());
                ServiceControlHandlerResult::NoError
            }
            _ => ServiceControlHandlerResult::NotImplemented,
        }
    };
    let status_handle = service_control_handler::register(SERVICE_NAME, event_handler)?;

    set_status(&status_handle, ServiceState::StartPending, 0)?;

    // Spawn a Tokio runtime for the actual daemon work.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");

    set_status(&status_handle, ServiceState::Running, 0)?;
    info!("service: Running");

    // Build a oneshot bridged from the std mpsc.
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
    std::thread::spawn(move || {
        let _ = stop_rx.recv();
        let _ = shutdown_tx.send(());
    });

    rt.block_on(async move {
        if let Err(e) = crate::run_daemon(shutdown_rx).await {
            error!("daemon error: {e:?}");
        }
    });

    set_status(&status_handle, ServiceState::Stopped, 0)?;
    info!("service: Stopped");
    Ok(())
}

fn set_status(
    handle: &service_control_handler::ServiceStatusHandle,
    state: ServiceState,
    checkpoint: u32,
) -> windows_service::Result<()> {
    let status = ServiceStatus {
        service_type: ServiceType::OWN_PROCESS,
        current_state: state,
        controls_accepted: ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN,
        exit_code: ServiceExitCode::Win32(0),
        checkpoint,
        wait_hint: Duration::from_secs(15),
        process_id: None,
    };
    handle.set_service_status(status)
}

/// Register the service with SCM. Requires elevation.
pub fn install() -> windows_service::Result<()> {
    let manager = ServiceManager::local_computer(
        None::<&str>,
        ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE,
    )?;
    let exe = std::env::current_exe().expect("current exe");

    let info = ServiceInfo {
        name: OsString::from(SERVICE_NAME),
        display_name: OsString::from(SERVICE_DISPLAY),
        service_type: ServiceType::OWN_PROCESS,
        start_type: ServiceStartType::AutoStart,
        error_control: ServiceErrorControl::Normal,
        executable_path: exe,
        launch_arguments: vec![OsString::from("--service")],
        dependencies: vec![],
        account_name: None, // LocalSystem
        account_password: None,
    };

    let service = manager.create_service(&info, ServiceAccess::CHANGE_CONFIG)?;
    service.set_description(SERVICE_DESCRIPTION)?;
    println!(
        "ULTRON installed as Windows Service '{SERVICE_NAME}' (auto-start).\n\
         Start it with:   sc start {SERVICE_NAME}\n\
         Stop it with:    sc stop  {SERVICE_NAME}\n\
         Uninstall:       ultron-core --uninstall  (elevated)"
    );
    Ok(())
}

/// Remove the service from SCM. Stops it first if running. Requires elevation.
pub fn uninstall() -> windows_service::Result<()> {
    let manager = ServiceManager::local_computer(
        None::<&str>,
        ServiceManagerAccess::CONNECT,
    )?;
    let svc = manager.open_service(
        SERVICE_NAME,
        ServiceAccess::QUERY_STATUS | ServiceAccess::STOP | ServiceAccess::DELETE,
    )?;
    let status = svc.query_status()?;
    if status.current_state != ServiceState::Stopped {
        let _ = svc.stop();
        // Best-effort wait.
        for _ in 0..30 {
            std::thread::sleep(Duration::from_millis(500));
            if let Ok(s) = svc.query_status() {
                if s.current_state == ServiceState::Stopped {
                    break;
                }
            }
        }
    }
    svc.delete()?;
    println!("ULTRON service '{SERVICE_NAME}' uninstalled.");
    Ok(())
}
