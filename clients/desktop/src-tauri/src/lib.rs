use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};
use std::{io::Read, process::Child};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

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
    #[serde(skip_serializing_if = "Option::is_none")]
    github_auth: Option<GithubAuthLoginDetails>,
}

#[derive(Serialize)]
struct GithubAuthLoginDetails {
    device_url: Option<String>,
    device_code: Option<String>,
    poll_interval_ms: u64,
    timeout_ms: u64,
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

/// Hand the per-launch server token to the webview so it can attach the
/// `X-Alfred-Token` header to a STREAMING POST it issues with the native
/// `fetch()` API. The buffered JSON helpers above attach the token in Rust, but
/// the converse token stream (#36) needs a streamed `ReadableStream` response
/// body, which only the webview's own `fetch` exposes (the Rust `reqwest` path
/// buffers the whole body before returning). `EventSource` cannot carry the
/// header and the route is a token-gated mutation, so the webview must present
/// the token itself. The token is already same-machine-readable (any process
/// running as the operator can read the 0600 file); surfacing it to our own
/// localhost-only webview does not widen that trust boundary.
#[tauri::command]
fn alfred_server_token() -> Result<String, String> {
    read_server_token().ok_or_else(|| {
        "could not read the Alfred server token; is the local runtime running?".to_string()
    })
}

#[tauri::command]
async fn run_alfred_action(
    action: String,
    target: Option<String>,
    cadence: Option<String>,
) -> Result<NativeCommandResult, String> {
    let action_name = action.trim();
    if action_name == "github_auth_login" {
        return start_github_auth_login().await;
    }

    if action_name == "brain_doctor" {
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

    let (program, args) = build_alfred_action(action_name, target.as_deref(), cadence.as_deref())?;
    if action_name == "code_memory_status" {
        return run_native_command_with_timeout(program, args, code_memory_doctor_timeout()).await;
    }
    run_native_command(program, args).await
}

#[tauri::command]
fn start_alfred_runtime(port: Option<u16>) -> Result<NativeCommandResult, String> {
    let port = port.unwrap_or(7010);
    if !(1024..=65535).contains(&port) {
        return Err("runtime port must be between 1024 and 65535".to_string());
    }

    let args = alfred_serve_args(port);
    let resolved = resolve_program("alfred");
    let child = command_with_cli_path(&resolved)
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
        github_auth: None,
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
        Some(text) if !text.trim().is_empty() => format!("Alfred fleet: {label}: {}", text.trim()),
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

    let mut builder = client.request(method.clone(), url);
    if method == Method::POST {
        // State-mutating POSTs must carry the per-launch token the server wrote
        // under $ALFRED_HOME/state/server-token. Without it the server returns
        // 403, so a drive-by same-origin localhost page (which cannot read the
        // 0600 token file) can never arm work or mutate fleet state.
        match read_server_token() {
            Some(token) => {
                builder = builder.header(SERVER_TOKEN_HEADER, token);
            }
            None => {
                return Err(
                    "could not read the Alfred server token; is the local runtime running?"
                        .to_string(),
                );
            }
        }
    }
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

/// Header the server requires on every state-mutating POST. It carries the
/// per-launch token written under `$ALFRED_HOME/state/server-token`.
const SERVER_TOKEN_HEADER: &str = "X-Alfred-Token";

/// Resolve the Alfred home directory the same way the Python runtime does:
/// `$ALFRED_HOME`, then `~/.alfred`.
fn alfred_home() -> Option<PathBuf> {
    if let Some(value) = config_value("ALFRED_HOME") {
        let trimmed = value.trim();
        if !trimmed.is_empty() {
            return Some(PathBuf::from(trimmed));
        }
    }
    home_dir().map(|home| home.join(".alfred"))
}

/// Best-effort home directory without pulling in an extra crate. Prefers
/// `$HOME` (set on macOS/Linux), then `$USERPROFILE` as a Windows fallback.
fn home_dir() -> Option<PathBuf> {
    for var in ["HOME", "USERPROFILE"] {
        if let Ok(value) = std::env::var(var) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return Some(PathBuf::from(trimmed));
            }
        }
    }
    None
}

/// Path to the per-launch server token under the resolved Alfred home.
fn server_token_path() -> Option<PathBuf> {
    alfred_home().map(|home| home.join("state").join("server-token"))
}

/// Read the per-launch server token the runtime wrote on start, if present.
fn read_server_token() -> Option<String> {
    let path = server_token_path()?;
    let raw = std::fs::read_to_string(path).ok()?;
    let token = raw.trim();
    if token.is_empty() {
        None
    } else {
        Some(token.to_string())
    }
}

fn code_memory_doctor_timeout() -> Duration {
    let fetch_budget = code_memory_launcher_u64(
        "ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S",
        CODE_MEMORY_FETCH_TIMEOUT_DEFAULT_S,
    );
    Duration::from_secs(fetch_budget.saturating_add(CODE_MEMORY_DOCTOR_TIMEOUT_MARGIN_S))
}

fn code_memory_launcher_u64(key: &str, default: u64) -> u64 {
    code_memory_launcher_config_value(key)
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(default)
}

fn code_memory_launcher_config_value(key: &str) -> Option<String> {
    let home = home_dir();
    let mut alfred_home = std::env::var("ALFRED_HOME")
        .ok()
        .and_then(non_empty_config_value)
        .map(PathBuf::from)
        .or_else(|| home.as_ref().map(|path| path.join(".alfred")));
    let mut value = std::env::var(key).ok().and_then(non_empty_config_value);

    if let Some(home) = home.as_ref() {
        load_launcher_env_value(&home.join(".alfredrc"), key, &mut value, &mut alfred_home);
    }
    if alfred_home.is_none() {
        alfred_home = home.as_ref().map(|path| path.join(".alfred"));
    }
    if let Some(path) = alfred_home.clone() {
        load_launcher_env_value(&path.join(".env"), key, &mut value, &mut alfred_home);
    }

    value
}

fn load_launcher_env_value(
    path: &Path,
    wanted_key: &str,
    wanted_value: &mut Option<String>,
    alfred_home: &mut Option<PathBuf>,
) {
    let Ok(raw) = std::fs::read_to_string(path) else {
        return;
    };
    for line in raw.lines() {
        let mut trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix("export ") {
            trimmed = rest.trim();
        }
        let Some((name, value)) = trimmed.split_once('=') else {
            continue;
        };
        let name = name.trim();
        if !is_launcher_env_key(name) {
            continue;
        }
        let Some(clean) = non_empty_config_value(value.trim().trim_matches('"').trim_matches('\''))
        else {
            continue;
        };
        let clean = expand_home_tokens(&clean);
        if name == wanted_key {
            *wanted_value = Some(clean.clone());
        }
        if name == "ALFRED_HOME" {
            *alfred_home = Some(PathBuf::from(clean));
        }
    }
}

fn is_launcher_env_key(value: &str) -> bool {
    let mut chars = value.chars();
    match chars.next() {
        Some(ch) if ch.is_ascii_alphabetic() || ch == '_' => {}
        _ => return false,
    }
    chars.all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

fn non_empty_config_value(value: impl AsRef<str>) -> Option<String> {
    let trimmed = value.as_ref().trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn expand_home_tokens(value: &str) -> String {
    let Some(home) = home_dir() else {
        return value.to_string();
    };
    let home = home.to_string_lossy();
    value.replace("${HOME}", &home).replace("$HOME", &home)
}

fn config_value(key: &str) -> Option<String> {
    let mut env = merged_alfred_env();
    env.remove(key)
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn merged_alfred_env() -> HashMap<String, String> {
    let mut env: HashMap<String, String> = std::env::vars().collect();
    let process_env_keys: HashSet<String> = env.keys().cloned().collect();
    let home = home_dir();
    load_selected_alfredrc_env(&mut env, home.as_deref(), &process_env_keys);

    let runtime_home = env
        .get("ALFRED_HOME")
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
        .or_else(|| home.as_ref().map(|home| home.join(".alfred")));
    if let Some(runtime_home) = runtime_home.as_deref() {
        env.entry("ALFRED_HOME".to_string())
            .or_insert_with(|| runtime_home.to_string_lossy().into_owned());
        load_config_file(
            &mut env,
            &runtime_home.join(".env"),
            true,
            false,
            home.as_deref(),
            &process_env_keys,
        );
    }
    if let Some(home) = home.as_deref() {
        env.entry("WORKSPACE_ROOT".to_string())
            .or_insert_with(|| home.join("code").to_string_lossy().into_owned());
    }
    env
}

fn load_selected_alfredrc_env(
    env: &mut HashMap<String, String>,
    home: Option<&Path>,
    process_env_keys: &HashSet<String>,
) {
    if let Some(alfredrc) = alfredrc_path(home, env) {
        load_config_file(env, &alfredrc, true, false, home, process_env_keys);
        if let Some(pointed_alfredrc) = alfredrc_path(home, env) {
            if pointed_alfredrc != alfredrc {
                env.insert(
                    "ALFREDRC".to_string(),
                    pointed_alfredrc.to_string_lossy().into_owned(),
                );
                load_config_file(env, &pointed_alfredrc, true, true, home, process_env_keys);
            }
        }
    }
}

fn alfredrc_path(home: Option<&Path>, env: &HashMap<String, String>) -> Option<PathBuf> {
    env.get("ALFREDRC")
        .map(|value| value.trim())
        .filter(|value| !value.is_empty())
        .map(|value| expand_home_path(value, home))
        .or_else(|| home.map(|home| home.join(".alfredrc")))
}

fn expand_home_path(value: &str, home: Option<&Path>) -> PathBuf {
    if value == "~" {
        if let Some(home) = home {
            return home.to_path_buf();
        }
    } else if let Some(rest) = value.strip_prefix("~/") {
        if let Some(home) = home {
            return home.join(rest);
        }
    }
    PathBuf::from(value)
}

fn load_config_file(
    env: &mut HashMap<String, String>,
    path: &Path,
    no_clobber: bool,
    file_overrides_existing: bool,
    home: Option<&Path>,
    process_env_keys: &HashSet<String>,
) {
    let Ok(raw) = std::fs::read_to_string(path) else {
        return;
    };
    for line in raw.lines() {
        let mut trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        if let Some(rest) = trimmed.strip_prefix("export ") {
            trimmed = rest.trim();
        }
        let Some((name, value)) = trimmed.split_once('=') else {
            continue;
        };
        let key = name.trim();
        if !is_valid_env_key(key) {
            continue;
        }
        let clean = decode_config_value(strip_inline_comment(value), home)
            .trim()
            .to_string();
        if overrides_with_stop_control(key, &clean) {
            env.insert(key.to_string(), clean);
            continue;
        }
        if no_clobber && process_env_keys.contains(key) {
            continue;
        }
        if let Some(existing) = env.get(key) {
            if preserves_stop_control(key, existing) {
                continue;
            }
        }
        if no_clobber && !file_overrides_existing && env.contains_key(key) {
            continue;
        }
        env.insert(key.to_string(), clean);
    }
}

fn is_valid_env_key(key: &str) -> bool {
    let mut chars = key.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if first.is_ascii_digit() {
        return false;
    }
    key.chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
}

fn strip_inline_comment(value: &str) -> &str {
    let mut quote: Option<char> = None;
    let mut escaped = false;
    for (index, ch) in value.char_indices() {
        if escaped {
            escaped = false;
            continue;
        }
        if ch == '\\' && quote != Some('\'') {
            escaped = true;
            continue;
        }
        if let Some(active) = quote {
            if ch == active {
                quote = None;
            }
            continue;
        }
        if ch == '\'' || ch == '"' {
            quote = Some(ch);
            continue;
        }
        let previous_is_space = index > 0
            && value[..index]
                .chars()
                .next_back()
                .is_some_and(|previous| previous.is_whitespace());
        if ch == '#' && previous_is_space {
            return value[..index].trim_end();
        }
    }
    value
}

fn preserves_stop_control(key: &str, value: &str) -> bool {
    let token = value.trim().to_ascii_lowercase();
    if token.is_empty() {
        return false;
    }
    if key == "ALFRED_AUTO_PROMOTE" {
        return !matches!(token.as_str(), "1" | "true" | "yes" | "on" | "enabled");
    }
    if key == "ALFRED_AUTO_PROMOTE_LLM_JUDGE" {
        return !matches!(token.as_str(), "1" | "true" | "yes" | "on" | "enabled");
    }
    key == "ALFRED_AUTO_PROMOTE_KILL"
        && !matches!(token.as_str(), "0" | "false" | "no" | "off" | "disabled")
}

fn overrides_with_stop_control(key: &str, value: &str) -> bool {
    preserves_stop_control(key, value)
}

fn decode_config_value(raw: &str, home: Option<&Path>) -> String {
    if raw.len() >= 2 && raw.starts_with('\'') && raw.ends_with('\'') {
        return raw[1..raw.len() - 1].replace("'\"'\"'", "'");
    }
    let value = if raw.len() >= 2 && raw.starts_with('"') && raw.ends_with('"') {
        raw[1..raw.len() - 1].to_string()
    } else {
        raw.to_string()
    };
    let Some(home) = home else {
        return value;
    };
    let home = home.to_string_lossy();
    value
        .replace("${HOME}", home.as_ref())
        .replace("$HOME", home.as_ref())
}

fn alfred_serve_args(port: u16) -> Vec<String> {
    vec![
        "serve".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--no-browser".to_string(),
    ]
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

    if url.port_or_known_default() == Some(7000) {
        url.set_port(Some(7010))
            .map_err(|_| "could not normalize local Alfred port".to_string())?;
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
        ch.is_ascii_alphanumeric() || matches!(ch, '/' | '?' | '&' | '=' | '.' | '_' | '-' | ':')
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
            || is_allowed_compose_converse(path_part)
            || is_allowed_conversation_control(path_part)
            || is_allowed_followup_action(path_part)
            || is_allowed_plan_decision(path_part)
            || is_allowed_memory_action(path_part)
            || is_allowed_slack_trust_action(path_part)
            || is_allowed_queue_action(path_part)
            || is_allowed_setup_action(path_part)
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
        "/api/memory/candidates",
        "/api/schedule",
        "/api/slack/trusted-users",
        "/api/shipped",
        "/api/usage",
        "/api/setup",
    ];
    allowed
        .iter()
        .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
}

fn is_allowed_compose_draft(path: &str) -> bool {
    // POST /api/plans/draft is the in-app spec/plan authoring endpoint.
    path == "/api/plans/draft"
}

fn is_allowed_compose_converse(path: &str) -> bool {
    // POST /api/compose/converse is the non-streaming chat fallback. The
    // token-streamed turn (/api/compose/converse/stream) rides the webview's
    // own fetch and never goes through this Rust bridge, so only the buffered
    // one-shot fallback needs to be on the contract here.
    path == "/api/compose/converse"
}

fn is_allowed_conversation_control(path: &str) -> bool {
    // POST /api/conversation/control carries the unified Ask composer's
    // conversational turns (status questions, control suggestions). Missing
    // from the contract, it broke chat sends in the packaged app while the
    // browser dev mode worked, because only the Rust bridge enforces this
    // allowlist.
    path == "/api/conversation/control"
}

fn is_allowed_setup_action(path: &str) -> bool {
    matches!(
        path,
        "/api/setup/repos" | "/api/setup/playbook" | "/api/setup/demo" | "/api/setup/demo/clear"
    )
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

fn is_allowed_plan_decision(path: &str) -> bool {
    // POST /api/plans/{id}/decision records an in-app approve/decline on a
    // genuine Batman plan. The {id} segment is parameterized like the
    // follow-up actions, so match the two-segment shape with a fixed verb.
    let Some(rest) = path.strip_prefix("/api/plans/") else {
        return false;
    };
    let parts: Vec<&str> = rest.split('/').collect();
    if parts.len() != 2 || parts[0].is_empty() {
        return false;
    }
    matches!(parts[1], "decision" | "file-issue" | "discard")
}

fn is_allowed_memory_action(path: &str) -> bool {
    let Some(rest) = path.strip_prefix("/api/memory/candidates/") else {
        return false;
    };
    let parts: Vec<&str> = rest.split('/').collect();
    if parts.len() != 2 || parts[0].is_empty() {
        return false;
    }
    matches!(parts[1], "promote" | "reject")
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

fn is_allowed_queue_action(path: &str) -> bool {
    // POST /api/queue is the queue/hold control endpoint. The desktop contract
    // only exposes the exact path; the server decides which actions are allowed.
    path == "/api/queue"
}

const GITHUB_DEVICE_URL: &str = "https://github.com/login/device";
const GITHUB_AUTH_CAPTURE_MS: u64 = 4_000;
const GITHUB_AUTH_POLL_INTERVAL_MS: u64 = 2_000;
const GITHUB_AUTH_TIMEOUT_MS: u64 = 120_000;
const CODE_MEMORY_FETCH_TIMEOUT_DEFAULT_S: u64 = 120;
const CODE_MEMORY_DOCTOR_TIMEOUT_MARGIN_S: u64 = 30;

fn resolve_gh_bin() -> String {
    if let Some(configured) = config_value("ALFRED_GH_BIN").or_else(|| config_value("GH_BIN")) {
        return configured;
    }
    for dir in cli_extra_paths() {
        let candidate = dir.join("gh");
        if candidate.is_file() {
            return candidate.to_string_lossy().to_string();
        }
    }
    "gh".to_string()
}

fn augmented_cli_path() -> std::ffi::OsString {
    let mut parts: Vec<PathBuf> = std::env::var_os("PATH")
        .map(|raw| std::env::split_paths(&raw).collect())
        .unwrap_or_default();
    for extra in cli_extra_paths().into_iter().rev() {
        if !parts.iter().any(|part| part == &extra) {
            parts.insert(0, extra);
        }
    }
    std::env::join_paths(parts).unwrap_or_else(|_| std::ffi::OsString::from(""))
}

fn cli_extra_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(home) = home_dir() {
        paths.push(home.join(".local").join("bin"));
        paths.push(home.join(".alfred").join("bin"));
    }
    paths.extend([
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/opt/homebrew/sbin"),
        PathBuf::from("/usr/local/bin"),
        PathBuf::from("/usr/local/sbin"),
    ]);
    paths
}

fn command_with_cli_path(program: &str) -> Command {
    let mut command = Command::new(program);
    command.envs(merged_alfred_env());
    command.env("PATH", augmented_cli_path());
    command
}

async fn start_github_auth_login() -> Result<NativeCommandResult, String> {
    tauri::async_runtime::spawn_blocking(start_github_auth_login_blocking)
        .await
        .map_err(|err| format!("GitHub sign-in failed to start: {err}"))?
}

fn start_github_auth_login_blocking() -> Result<NativeCommandResult, String> {
    let first = start_github_auth_login_attempt(true)?;
    if first.success || !is_unknown_clipboard_flag(&first) {
        return Ok(first);
    }
    start_github_auth_login_attempt(false)
}

fn github_auth_login_args(include_clipboard: bool) -> Vec<String> {
    let mut args = vec![
        "auth".to_string(),
        "login".to_string(),
        "--hostname".to_string(),
        "github.com".to_string(),
        "--git-protocol".to_string(),
        "https".to_string(),
        "--web".to_string(),
    ];
    if include_clipboard {
        args.push("--clipboard".to_string());
    }
    args
}

fn start_github_auth_login_attempt(include_clipboard: bool) -> Result<NativeCommandResult, String> {
    let args = github_auth_login_args(include_clipboard);
    let gh = resolve_gh_bin();
    let preview = command_preview(&gh, &args);
    let mut child = command_with_cli_path(&gh)
        .args(&args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("could not start GitHub sign-in: {err}"))?;

    let pid = child.id();
    let (tx, rx) = mpsc::channel::<(bool, String)>();
    if let Some(stdout) = child.stdout.take() {
        spawn_pipe_reader(stdout, true, tx.clone());
    }
    if let Some(stderr) = child.stderr.take() {
        spawn_pipe_reader(stderr, false, tx.clone());
    }
    drop(tx);

    let started = Instant::now();
    let mut stdout = String::new();
    let mut stderr = String::new();
    let mut status = None;
    let mut success = true;
    let mut exited = false;

    while started.elapsed() < Duration::from_millis(GITHUB_AUTH_CAPTURE_MS) {
        let mut pipes_disconnected = false;
        match rx.recv_timeout(Duration::from_millis(120)) {
            Ok((is_stdout, chunk)) => {
                if is_stdout {
                    stdout.push_str(&chunk);
                } else {
                    stderr.push_str(&chunk);
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                pipes_disconnected = true;
            }
        }

        if let Some(exit) = child
            .try_wait()
            .map_err(|err| format!("could not check GitHub sign-in status: {err}"))?
        {
            status = exit.code();
            success = exit.success();
            exited = true;
            break;
        }

        if github_auth_capture_should_stop(pipes_disconnected, &stdout, &stderr) {
            break;
        }
    }

    drain_pipe_chunks(&rx, &mut stdout, &mut stderr);

    if !exited {
        reap_child(child);
    }

    let combined = combined_output(&stdout, &stderr);
    let device_code = extract_device_code(&combined);
    let device_url = extract_first_url(&combined).or_else(|| Some(GITHUB_DEVICE_URL.to_string()));
    let message = if exited && success {
        "GitHub sign-in completed.".to_string()
    } else if exited {
        "GitHub sign-in exited before Alfred could confirm authentication.".to_string()
    } else if device_code.is_some() {
        "GitHub sign-in started. Enter the one-time code in your browser.".to_string()
    } else {
        "GitHub sign-in started. Finish the browser prompt, then Alfred will recheck.".to_string()
    };

    Ok(NativeCommandResult {
        command: preview,
        stdout: trim_text(&stdout),
        stderr: trim_text(&stderr),
        status,
        success: success || !exited,
        pid: Some(pid),
        message: Some(message),
        github_auth: Some(GithubAuthLoginDetails {
            device_url,
            device_code,
            poll_interval_ms: GITHUB_AUTH_POLL_INTERVAL_MS,
            timeout_ms: GITHUB_AUTH_TIMEOUT_MS,
        }),
    })
}

fn is_unknown_clipboard_flag(result: &NativeCommandResult) -> bool {
    let haystack = format!("{}\n{}", result.stdout, result.stderr).to_ascii_lowercase();
    haystack.contains("clipboard")
        && (haystack.contains("unknown flag")
            || haystack.contains("unknown shorthand")
            || haystack.contains("flag provided but not defined"))
}

fn spawn_pipe_reader<R>(mut reader: R, is_stdout: bool, tx: mpsc::Sender<(bool, String)>)
where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut buffer = [0_u8; 1024];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(n) => {
                    let chunk = String::from_utf8_lossy(&buffer[..n]).to_string();
                    let _ = tx.send((is_stdout, chunk));
                }
                Err(_) => break,
            }
        }
    });
}

fn drain_pipe_chunks(
    rx: &mpsc::Receiver<(bool, String)>,
    stdout: &mut String,
    stderr: &mut String,
) {
    while let Ok((is_stdout, chunk)) = rx.try_recv() {
        if is_stdout {
            stdout.push_str(&chunk);
        } else {
            stderr.push_str(&chunk);
        }
    }
}

fn github_auth_capture_should_stop(pipes_disconnected: bool, stdout: &str, stderr: &str) -> bool {
    extract_device_code(&combined_output(stdout, stderr)).is_some() || pipes_disconnected
}

fn reap_child(mut child: Child) {
    thread::spawn(move || {
        let _ = child.wait();
    });
}

fn combined_output(stdout: &str, stderr: &str) -> String {
    format!("{stdout}\n{stderr}")
}

fn extract_first_url(text: &str) -> Option<String> {
    text.split_whitespace().find_map(|token| {
        let clean = token.trim_matches(|ch: char| {
            matches!(
                ch,
                '<' | '>' | '(' | ')' | '[' | ']' | ',' | '.' | ';' | '"' | '\''
            )
        });
        if clean.starts_with("https://") || clean.starts_with("http://") {
            Some(clean.to_string())
        } else {
            None
        }
    })
}

fn extract_device_code(text: &str) -> Option<String> {
    for line in text.lines() {
        let lower = line.to_ascii_lowercase();
        if lower.contains("code") {
            for token in line.split_whitespace().rev() {
                let clean = clean_device_token(token);
                if looks_like_device_code(&clean) {
                    return Some(clean);
                }
            }
        }
    }
    text.split_whitespace()
        .map(clean_device_token)
        .find(|token| looks_like_device_code(token))
}

fn clean_device_token(token: &str) -> String {
    token
        .trim_matches(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '-'))
        .to_string()
}

fn looks_like_device_code(value: &str) -> bool {
    let len = value.len();
    (6..=24).contains(&len)
        && value.contains('-')
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-')
}

fn build_alfred_action(
    action: &str,
    target: Option<&str>,
    cadence: Option<&str>,
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
        "schedule" => {
            let codename = validate_codename(
                target.ok_or_else(|| "schedule needs an agent codename".to_string())?,
            )?;
            let cadence = validate_schedule_cadence(
                cadence.ok_or_else(|| "schedule needs a cadence".to_string())?,
            )?;
            Ok((
                "alfred".to_string(),
                vec!["schedule".to_string(), "set".to_string(), codename, cadence],
            ))
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
        "code_memory_status" => Ok((
            "alfred".to_string(),
            vec!["code-memory".to_string(), "doctor".to_string()],
        )),
        "redis_status" => Ok((
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "redis-status".to_string(),
                "--json".to_string(),
            ],
        )),
        "redis_sync_preview" => Ok((
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "redis-sync".to_string(),
                "--dry-run".to_string(),
                "--json".to_string(),
            ],
        )),
        "memory_harvest" => Ok((
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "harvest".to_string(),
                "--apply".to_string(),
                "--json".to_string(),
            ],
        )),
        "memory_auto_promote" => Ok((
            "alfred".to_string(),
            vec![
                "brain".to_string(),
                "auto-promote".to_string(),
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
    run_native_command_with_timeout(program, args, Duration::from_secs(0)).await
}

async fn run_native_command_with_timeout(
    program: String,
    args: Vec<String>,
    timeout: Duration,
) -> Result<NativeCommandResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_native_command_blocking(program, args, timeout)
    })
    .await
    .map_err(|err| format!("native action failed to complete: {err}"))?
}

fn run_native_command_blocking(
    program: String,
    args: Vec<String>,
    timeout: Duration,
) -> Result<NativeCommandResult, String> {
    let preview = command_preview(&program, &args);
    let resolved = resolve_program(&program);
    let mut command = command_with_cli_path(&resolved);
    command.args(&args).stdin(Stdio::null());
    if timeout.is_zero() {
        let output = command.output().map_err(|err| {
            format!(
                "could not run {} (resolved to {resolved}): {err}",
                preview.join(" ")
            )
        })?;
        return Ok(NativeCommandResult {
            command: preview,
            stdout: trim_output(&output.stdout),
            stderr: trim_output(&output.stderr),
            status: output.status.code(),
            success: output.status.success(),
            pid: None,
            message: None,
            github_auth: None,
        });
    }

    #[cfg(unix)]
    {
        command.process_group(0);
    }
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| {
            format!(
                "could not run {} (resolved to {resolved}): {err}",
                preview.join(" ")
            )
        })?;
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let (stdout, stderr) = read_child_output(&mut child);
                return Ok(NativeCommandResult {
                    command: preview,
                    stdout: trim_output(&stdout),
                    stderr: trim_output(&stderr),
                    status: status.code(),
                    success: status.success(),
                    pid: None,
                    message: None,
                    github_auth: None,
                });
            }
            Ok(None) if started.elapsed() >= timeout => {
                terminate_child_tree(&mut child);
                let (stdout, stderr) = read_child_output(&mut child);
                let timeout_msg = format!("command timed out after {}", duration_label(timeout));
                let stderr = if stderr.is_empty() {
                    timeout_msg.clone().into_bytes()
                } else {
                    let mut combined = stderr;
                    combined.extend_from_slice(b"\n");
                    combined.extend_from_slice(timeout_msg.as_bytes());
                    combined
                };
                return Ok(NativeCommandResult {
                    command: preview,
                    stdout: trim_output(&stdout),
                    stderr: trim_output(&stderr),
                    status: Some(124),
                    success: false,
                    pid: None,
                    message: Some(timeout_msg),
                    github_auth: None,
                });
            }
            Ok(None) => thread::sleep(Duration::from_millis(100)),
            Err(err) => {
                terminate_child_tree(&mut child);
                return Err(format!("native action status check failed: {err}"));
            }
        }
    }
}

fn duration_label(duration: Duration) -> String {
    if duration.as_secs() > 0 {
        format!("{}s", duration.as_secs())
    } else {
        format!("{}ms", duration.as_millis())
    }
}

fn read_child_output(child: &mut Child) -> (Vec<u8>, Vec<u8>) {
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    if let Some(mut pipe) = child.stdout.take() {
        let _ = pipe.read_to_end(&mut stdout);
    }
    if let Some(mut pipe) = child.stderr.take() {
        let _ = pipe.read_to_end(&mut stderr);
    }
    (stdout, stderr)
}

fn terminate_child_tree(child: &mut Child) {
    #[cfg(unix)]
    {
        let group = format!("-{}", child.id());
        let _ = Command::new("/bin/kill").arg("-TERM").arg(&group).status();
        thread::sleep(Duration::from_millis(200));
        if matches!(child.try_wait(), Ok(None)) {
            let _ = Command::new("/bin/kill").arg("-KILL").arg(&group).status();
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn resolve_program(requested: &str) -> String {
    if requested != "alfred" {
        return requested.to_string();
    }

    let explicit = config_value("ALFRED_BIN");
    let runtime_home = alfred_home();
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let install_candidates = [
        PathBuf::from("/opt/homebrew/bin/alfred"),
        PathBuf::from("/usr/local/bin/alfred"),
    ];

    resolve_alfred_program(
        explicit.as_deref(),
        runtime_home.as_deref(),
        home.as_deref(),
        &install_candidates,
    )
}

fn resolve_alfred_program(
    explicit: Option<&str>,
    runtime_home: Option<&Path>,
    home: Option<&Path>,
    install_candidates: &[PathBuf],
) -> String {
    if let Some(raw) = explicit {
        let trimmed = raw.trim();
        if !trimmed.is_empty() && Path::new(trimmed).is_file() {
            return trimmed.to_string();
        }
    }

    if let Some(runtime_home) = runtime_home {
        let candidate = runtime_home.join("bin").join("alfred");
        if candidate.is_file() {
            return candidate.to_string_lossy().into_owned();
        }
    }

    if let Some(home) = home {
        for relative in [".local/bin/alfred", ".alfred/bin/alfred"] {
            let candidate = home.join(relative);
            if candidate.is_file() {
                return candidate.to_string_lossy().into_owned();
            }
        }
    }

    for candidate in install_candidates {
        if candidate.is_file() {
            return candidate.to_string_lossy().into_owned();
        }
    }

    "alfred".to_string()
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

fn validate_schedule_cadence(value: &str) -> Result<String, String> {
    let clean = value.trim();
    if clean.is_empty() || clean.len() > 80 {
        return Err("schedule cadence is missing or too long".to_string());
    }
    if clean.starts_with('-') {
        return Err("schedule cadence may not start with a hyphen".to_string());
    }
    if !clean
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, ':' | '@' | '_' | '-'))
    {
        return Err("schedule cadence contains unsupported characters".to_string());
    }
    Ok(clean.to_string())
}

fn command_preview(program: &str, args: &[String]) -> Vec<String> {
    let mut command = vec![program.to_string()];
    command.extend(args.iter().cloned());
    command
}

fn trim_output(bytes: &[u8]) -> String {
    trim_text(&String::from_utf8_lossy(bytes))
}

fn trim_text(text: &str) -> String {
    const MAX_CHARS: usize = 20_000;
    if text.chars().count() <= MAX_CHARS {
        return text.to_string();
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
            alfred_server_token,
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
    use std::fs::{self, File};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock should be after unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "alfred-desktop-{name}-{}-{stamp}",
            std::process::id()
        ))
    }

    fn touch(path: &Path) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("test dir should be created");
        }
        File::create(path).expect("test file should be created");
    }

    #[test]
    fn resolve_program_passes_through_non_alfred_programs() {
        assert_eq!(resolve_program("gh"), "gh");
        assert_eq!(resolve_program("/usr/bin/env"), "/usr/bin/env");
    }

    #[test]
    fn resolve_alfred_program_prefers_existing_explicit_override() {
        let root = temp_root("explicit");
        let explicit = root.join("bin").join("alfred");
        touch(&explicit);

        let resolved = resolve_alfred_program(
            Some(explicit.to_str().expect("test path should be utf-8")),
            None,
            None,
            &[],
        );

        assert_eq!(resolved, explicit.to_string_lossy().into_owned());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn resolve_alfred_program_uses_home_install_paths() {
        let root = temp_root("home");
        let local = root.join(".local").join("bin").join("alfred");
        touch(&local);

        let resolved = resolve_alfred_program(None, None, Some(&root), &[]);

        assert_eq!(resolved, local.to_string_lossy().into_owned());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn resolve_alfred_program_prefers_configured_runtime_home() {
        let root = temp_root("runtime-home");
        let runtime_home = root.join("configured");
        let user_home = root.join("user");
        let runtime = runtime_home.join("bin").join("alfred");
        let local = user_home.join(".local").join("bin").join("alfred");
        touch(&local);
        touch(&runtime);

        let resolved = resolve_alfred_program(None, Some(&runtime_home), Some(&user_home), &[]);

        assert_eq!(resolved, runtime.to_string_lossy().into_owned());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn resolve_alfred_program_uses_global_install_candidates() {
        let root = temp_root("global");
        let global = root.join("alfred");
        touch(&global);

        let resolved = resolve_alfred_program(None, None, None, &[global.clone()]);

        assert_eq!(resolved, global.to_string_lossy().into_owned());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn resolve_alfred_program_falls_back_to_path_lookup() {
        let missing = temp_root("missing").join("alfred");

        let resolved = resolve_alfred_program(
            Some("/definitely/not/a/real/alfred"),
            None,
            None,
            &[missing],
        );

        assert_eq!(resolved, "alfred");
    }

    #[test]
    fn base_url_rewrites_legacy_airplay_port() {
        let url = validate_base_url("http://127.0.0.1:7000")
            .expect("legacy localhost port should still be accepted");
        assert_eq!(url.as_str(), "http://127.0.0.1:7010/");
    }

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
    fn queue_api_paths_are_allowlisted() {
        let (path, query) = validate_api_path("/api/queue", &Method::POST)
            .expect("queue action path should be accepted for POST");
        assert_eq!(path, "/api/queue");
        assert_eq!(query, None);

        assert!(is_allowed_queue_action("/api/queue"));
        assert!(!is_allowed_queue_action("/api/queue/extra"));
        assert!(!is_allowed_queue_action("/api/queues"));

        // Only POST is allowed; a GET to /api/queue is not on the read contract.
        let err = validate_api_path("/api/queue", &Method::GET)
            .expect_err("queue must not be reachable via GET");
        assert!(err.contains("desktop contract"));
    }

    #[test]
    fn desktop_runtime_start_keeps_browser_closed() {
        assert_eq!(
            alfred_serve_args(7010),
            vec![
                "serve".to_string(),
                "--port".to_string(),
                "7010".to_string(),
                "--no-browser".to_string(),
            ]
        );
    }

    #[test]
    fn memory_candidate_api_paths_are_allowlisted() {
        let (path, query) = validate_api_path("/api/memory/candidates?limit=20", &Method::GET)
            .expect("memory candidates list should be accepted for GET");
        assert_eq!(path, "/api/memory/candidates");
        assert_eq!(query, Some("limit=20"));

        let (path, _query) = validate_api_path(
            "/api/memory/candidates/mem:candidate_123/promote",
            &Method::POST,
        )
        .expect("memory promote path should be accepted for POST");
        assert_eq!(path, "/api/memory/candidates/mem:candidate_123/promote");

        let err = validate_api_path("/api/memory/candidates/candidate_123/delete", &Method::POST)
            .expect_err("unlisted memory actions must stay blocked");
        assert!(err.contains("desktop contract"));
    }

    #[test]
    fn get_allowlist_accepts_compose_drafts_list_path() {
        let (path, query) = validate_api_path("/api/plans/drafts", &Method::GET)
            .expect("compose drafts list path should be accepted for GET");
        assert_eq!(path, "/api/plans/drafts");
        assert_eq!(query, None);
    }

    #[test]
    fn get_allowlist_accepts_shipped_board_path() {
        let (path, query) = validate_api_path("/api/shipped?days=14", &Method::GET)
            .expect("shipped board path should be accepted for GET");
        assert_eq!(path, "/api/shipped");
        assert_eq!(query, Some("days=14"));
    }

    #[test]
    fn get_allowlist_accepts_usage_path() {
        let (path, query) = validate_api_path("/api/usage", &Method::GET)
            .expect("usage path should be accepted for GET");
        assert_eq!(path, "/api/usage");
        assert_eq!(query, None);
    }

    #[test]
    fn get_allowlist_accepts_schedule_path() {
        let (path, query) = validate_api_path("/api/schedule", &Method::GET)
            .expect("schedule path should be accepted for GET");
        assert_eq!(path, "/api/schedule");
        assert_eq!(query, None);
    }

    #[test]
    fn setup_api_paths_are_allowlisted() {
        for read in [
            "/api/setup/status",
            "/api/setup/repos",
            "/api/setup/repos?limit=50",
            "/api/setup/playbooks",
        ] {
            validate_api_path(read, &Method::GET).expect("setup reads are allowed");
        }

        for write in [
            "/api/setup/repos",
            "/api/setup/playbook",
            "/api/setup/demo",
            "/api/setup/demo/clear",
        ] {
            assert!(is_allowed_setup_action(write));
            validate_api_path(write, &Method::POST).expect("setup writes are allowed");
        }

        assert!(!is_allowed_setup_action("/api/setup/wipe"));
        let err = validate_api_path("/api/setup/wipe", &Method::POST)
            .expect_err("unknown setup write must stay blocked");
        assert!(err.contains("desktop contract"));
    }

    #[test]
    fn plan_decision_and_compose_converse_are_allowlisted() {
        // POST /api/plans/{id}/decision (approve/decline) is on the contract,
        // with the {id} segment parameterized like the follow-up actions.
        let (path, query) = validate_api_path("/api/plans/batman-42/decision", &Method::POST)
            .expect("plan decision path should be accepted for POST");
        assert_eq!(path, "/api/plans/batman-42/decision");
        assert_eq!(query, None);

        assert!(is_allowed_plan_decision("/api/plans/batman-42/decision"));
        let (path, query) = validate_api_path("/api/plans/compose-123/file-issue", &Method::POST)
            .expect("plan file-issue path should be accepted for POST");
        assert_eq!(path, "/api/plans/compose-123/file-issue");
        assert_eq!(query, None);
        assert!(is_allowed_plan_decision(
            "/api/plans/compose-123/file-issue"
        ));
        let (path, query) = validate_api_path("/api/plans/compose-123/discard", &Method::POST)
            .expect("plan discard path should be accepted for POST");
        assert_eq!(path, "/api/plans/compose-123/discard");
        assert_eq!(query, None);
        assert!(is_allowed_plan_decision("/api/plans/compose-123/discard"));
        // The single-segment draft path and unknown verbs must stay off the rule.
        assert!(!is_allowed_plan_decision("/api/plans/draft"));
        assert!(!is_allowed_plan_decision("/api/plans/batman-42/delete"));
        assert!(!is_allowed_plan_decision(
            "/api/plans/batman-42/decision/extra"
        ));

        // POST /api/compose/converse is the buffered chat fallback.
        let (path, _query) = validate_api_path("/api/compose/converse", &Method::POST)
            .expect("compose converse fallback should be accepted for POST");
        assert_eq!(path, "/api/compose/converse");
        assert!(is_allowed_compose_converse("/api/compose/converse"));
        assert!(is_allowed_conversation_control("/api/conversation/control"));
        assert!(!is_allowed_conversation_control(
            "/api/conversation/control/x"
        ));
        // The streamed variant never rides this Rust bridge, so it is NOT on
        // the buffered POST contract here.
        assert!(!is_allowed_compose_converse("/api/compose/converse/stream"));

        // /api/compose is not a read route, so a GET stays blocked. (Plan paths
        // share the /api/plans read prefix, so a GET there is intentionally fine.)
        let err = validate_api_path("/api/compose/converse", &Method::GET)
            .expect_err("compose converse must not be reachable via GET");
        assert!(err.contains("desktop contract"));
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
            let (program, args) = build_alfred_action(action, Some("lucius"), None)
                .expect("valid codename is accepted");
            assert_eq!(program, "alfred");
            assert_eq!(args, vec![verb.to_string(), "lucius".to_string()]);
        }
    }

    #[test]
    fn fleet_control_accepts_fully_qualified_codename() {
        let (_, args) = build_alfred_action("pause", Some("example.fleet.lucius"), None)
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
            build_alfred_action("pause", Some("all"), None).expect("pause all is accepted");
        assert_eq!(pause_args, vec!["pause".to_string(), "all".to_string()]);
        let (_, resume_args) =
            build_alfred_action("resume", Some("all"), None).expect("resume all is accepted");
        assert_eq!(resume_args, vec!["resume".to_string(), "all".to_string()]);
        // `run all` is not rejected (the CLI itself decides), but `all` is just
        // passed through as a single, validated argument, never expanded here.
        let (_, run_args) =
            build_alfred_action("run", Some("all"), None).expect("run all passes through");
        assert_eq!(run_args, vec!["run".to_string(), "all".to_string()]);
    }

    #[test]
    fn schedule_action_builds_fixed_set_command() {
        let (program, args) = build_alfred_action("schedule", Some("lucius"), Some("20m"))
            .expect("schedule cadence is accepted");
        assert_eq!(program, "alfred");
        assert_eq!(
            args,
            vec![
                "schedule".to_string(),
                "set".to_string(),
                "lucius".to_string(),
                "20m".to_string(),
            ]
        );

        let (_, weekly_args) =
            build_alfred_action("schedule", Some("batman"), Some("weekly@mon:09:00"))
                .expect("weekly cadence is accepted");
        assert_eq!(
            weekly_args,
            vec![
                "schedule".to_string(),
                "set".to_string(),
                "batman".to_string(),
                "weekly@mon:09:00".to_string(),
            ]
        );
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
                let err = build_alfred_action(action, Some(bad), None)
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
            let err = build_alfred_action(action, None, None)
                .expect_err("a missing target must be rejected");
            assert!(err.contains("needs an agent codename"));
        }
    }

    #[test]
    fn schedule_action_rejects_bad_cadence() {
        let err = build_alfred_action("schedule", Some("lucius"), None)
            .expect_err("schedule needs cadence");
        assert!(err.contains("cadence"));

        for bad in [
            "--force",
            "10m && whoami",
            "daily@09:00 extra",
            "weekly/mon",
        ] {
            let err = build_alfred_action("schedule", Some("lucius"), Some(bad))
                .expect_err("bad cadence must be rejected");
            assert!(
                err.contains("cadence"),
                "schedule should reject {bad:?} with a cadence error, got: {err}"
            );
        }
    }

    #[test]
    fn unknown_actions_are_still_rejected() {
        let err = build_alfred_action("destroy", Some("lucius"), None)
            .expect_err("unlisted verbs must not pass the allowlist");
        assert!(err.contains("unknown native Alfred action"));
    }

    #[test]
    fn github_auth_parser_extracts_device_code_and_url() {
        let text = "! First copy your one-time code: ABCD-1234\nThen open: https://github.com/login/device\n";
        assert_eq!(extract_device_code(text).as_deref(), Some("ABCD-1234"));
        assert_eq!(
            extract_first_url(text).as_deref(),
            Some("https://github.com/login/device")
        );
    }

    #[test]
    fn github_auth_parser_trims_terminal_punctuation() {
        let text = "Copy code `WXYZ-9876`, then visit <https://github.com/login/device>.";
        assert_eq!(extract_device_code(text).as_deref(), Some("WXYZ-9876"));
        assert_eq!(
            extract_first_url(text).as_deref(),
            Some("https://github.com/login/device")
        );
    }

    #[test]
    fn github_auth_capture_stops_on_device_code_or_closed_pipes() {
        assert!(github_auth_capture_should_stop(
            false,
            "First copy your one-time code: ABCD-1234",
            ""
        ));
        assert!(github_auth_capture_should_stop(true, "", ""));
        assert!(!github_auth_capture_should_stop(false, "", "waiting"));
    }

    #[test]
    fn github_auth_login_args_can_drop_clipboard_for_old_gh() {
        let with_clipboard = github_auth_login_args(true);
        assert!(with_clipboard.contains(&"--clipboard".to_string()));
        let without_clipboard = github_auth_login_args(false);
        assert!(!without_clipboard.contains(&"--clipboard".to_string()));
        assert!(without_clipboard.contains(&"--web".to_string()));
        assert!(without_clipboard.contains(&"--hostname".to_string()));
        assert!(without_clipboard.contains(&"github.com".to_string()));
    }

    #[test]
    fn github_auth_detects_unknown_clipboard_flag() {
        let result = NativeCommandResult {
            command: vec!["gh".to_string(), "auth".to_string()],
            stdout: String::new(),
            stderr: "unknown flag: --clipboard".to_string(),
            status: Some(1),
            success: false,
            pid: None,
            message: None,
            github_auth: None,
        };
        assert!(is_unknown_clipboard_flag(&result));
    }

    #[test]
    fn memory_native_actions_build_fixed_commands() {
        let (_, code_memory_args) = build_alfred_action("code_memory_status", None, None)
            .expect("code-memory status has no target");
        assert_eq!(
            code_memory_args,
            vec!["code-memory".to_string(), "doctor".to_string(),]
        );

        let (_, redis_args) = build_alfred_action("redis_sync_preview", None, None)
            .expect("redis preview has no target");
        assert_eq!(
            redis_args,
            vec![
                "brain".to_string(),
                "redis-sync".to_string(),
                "--dry-run".to_string(),
                "--json".to_string(),
            ]
        );

        let (_, harvest_args) = build_alfred_action("memory_harvest", None, None)
            .expect("memory harvest has no target");
        assert_eq!(
            harvest_args,
            vec![
                "brain".to_string(),
                "harvest".to_string(),
                "--apply".to_string(),
                "--json".to_string(),
            ]
        );

        let (_, promote_args) = build_alfred_action("memory_auto_promote", None, None)
            .expect("memory auto-promote has no target");
        assert_eq!(
            promote_args,
            vec![
                "brain".to_string(),
                "auto-promote".to_string(),
                "--json".to_string(),
            ]
        );
    }

    #[test]
    fn native_command_timeout_returns_bounded_failure() {
        let result = run_native_command_blocking(
            "/bin/sh".to_string(),
            vec!["-c".to_string(), "sleep 2".to_string()],
            Duration::from_millis(50),
        )
        .expect("timeout result should be captured");

        assert!(!result.success);
        assert_eq!(result.status, Some(124));
        assert_eq!(
            result.message.as_deref(),
            Some("command timed out after 50ms")
        );
        assert!(result.stderr.contains("command timed out after 50ms"));
    }

    #[test]
    fn code_memory_doctor_timeout_uses_default_fetch_budget() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_fetch = std::env::var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S").ok();

        let root = temp_root("code-memory-timeout-default");
        let home = root.join("home");
        fs::create_dir_all(&home).expect("create temp home");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S");

        assert_eq!(code_memory_doctor_timeout(), Duration::from_secs(150));

        let _ = fs::remove_dir_all(root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S", prev_fetch);
    }

    #[test]
    fn code_memory_doctor_timeout_reads_alfredrc_fetch_budget() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_fetch = std::env::var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S").ok();

        let root = temp_root("code-memory-timeout-rc");
        let home = root.join("home");
        fs::create_dir_all(&home).expect("create temp home");
        fs::write(
            home.join(".alfredrc"),
            "ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S='240'\n",
        )
        .expect("write rc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S");

        assert_eq!(code_memory_doctor_timeout(), Duration::from_secs(270));

        let _ = fs::remove_dir_all(root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S", prev_fetch);
    }

    #[test]
    fn code_memory_doctor_timeout_env_file_overrides_alfredrc_budget() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_fetch = std::env::var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S").ok();

        let root = temp_root("code-memory-timeout-env");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create runtime home");
        fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_CODE_MEMORY_FETCH_TIMEOUT_S=90\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write rc");
        fs::write(
            runtime.join(".env"),
            "ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S=300\n",
        )
        .expect("write runtime env");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S");

        assert_eq!(code_memory_doctor_timeout(), Duration::from_secs(330));

        let _ = fs::remove_dir_all(root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFRED_CODE_MEMORY_FETCH_TIMEOUT_S", prev_fetch);
    }

    #[test]
    fn validate_codename_caps_length() {
        let long = "a".repeat(81);
        assert!(validate_codename(&long).is_err());
        let ok = "a".repeat(80);
        assert_eq!(validate_codename(&ok).expect("80 chars is allowed"), ok);
    }

    #[test]
    fn alfred_resolver_prefers_configured_env_var() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred_bin = std::env::var("ALFRED_BIN").ok();

        let root = temp_root("alfred-bin-env");
        let configured = root.join("bin").join("alfred");
        touch(&configured);

        std::env::set_var("ALFRED_BIN", &configured);
        assert_eq!(
            resolve_program("alfred"),
            configured.to_string_lossy().into_owned()
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("ALFRED_BIN", prev_alfred_bin);
    }

    #[test]
    fn alfred_resolver_reads_alfred_env_file() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_alfred_bin = std::env::var("ALFRED_BIN").ok();

        let root = temp_root("alfred-bin-dotenv");
        let configured = root.join("custom").join("alfred");
        touch(&configured);
        std::fs::write(
            root.join(".env"),
            format!("ALFRED_BIN='{}'\n", configured.to_string_lossy()),
        )
        .expect("write temp env");

        std::env::set_var("ALFRED_HOME", &root);
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_BIN");
        assert_eq!(
            resolve_program("alfred"),
            configured.to_string_lossy().into_owned()
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_BIN", prev_alfred_bin);
    }

    #[test]
    fn alfred_resolver_reads_alfredrc() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_alfred_bin = std::env::var("ALFRED_BIN").ok();

        let root = temp_root("alfred-bin-alfredrc");
        let home = root.join("home");
        let configured = root.join("custom").join("alfred");
        fs::create_dir_all(&home).expect("create temp home");
        touch(&configured);
        std::fs::write(
            home.join(".alfredrc"),
            format!("ALFRED_BIN='{}'\n", configured.to_string_lossy()),
        )
        .expect("write temp alfredrc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_BIN");
        assert_eq!(
            resolve_program("alfred"),
            configured.to_string_lossy().into_owned()
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_BIN", prev_alfred_bin);
    }

    #[test]
    fn native_subprocess_env_honors_custom_alfredrc() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-custom-alfredrc");
        let home = root.join("home");
        let stale_runtime = root.join("stale-runtime");
        let runtime = root.join("runtime");
        let custom_rc = root.join("custom.alfredrc");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&stale_runtime).expect("create stale runtime");
        fs::create_dir_all(&runtime).expect("create runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=1\nALFRED_AUTO_PROMOTE_KILL=0\n",
                stale_runtime.to_string_lossy()
            ),
        )
        .expect("write stale home alfredrc");
        std::fs::write(
            &custom_rc,
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write custom alfredrc");

        std::env::set_var("HOME", &home);
        std::env::set_var("ALFREDRC", &custom_rc);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFRED_AUTO_PROMOTE");
        std::env::remove_var("ALFRED_AUTO_PROMOTE_KILL");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFRED_HOME"),
            Some(&runtime.to_string_lossy().to_string())
        );
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE_KILL"), Some(&"1".to_string()));

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn native_subprocess_env_follows_persisted_alfredrc_pointer() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-persisted-alfredrc-pointer");
        let home = root.join("home");
        let stale_runtime = root.join("stale-runtime");
        let runtime = root.join("runtime");
        let custom_rc = root.join("custom.alfredrc");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&stale_runtime).expect("create stale runtime");
        fs::create_dir_all(&runtime).expect("create runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFREDRC='{}'\nALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=1\n\
                 ALFRED_AUTO_PROMOTE_KILL=0\n",
                custom_rc.to_string_lossy(),
                stale_runtime.to_string_lossy()
            ),
        )
        .expect("write home alfredrc pointer");
        std::fs::write(
            &custom_rc,
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write custom alfredrc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_AUTO_PROMOTE");
        std::env::remove_var("ALFRED_AUTO_PROMOTE_KILL");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFREDRC"),
            Some(&custom_rc.to_string_lossy().to_string())
        );
        assert_eq!(
            env.get("ALFRED_HOME"),
            Some(&runtime.to_string_lossy().to_string())
        );
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE_KILL"), Some(&"1".to_string()));

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn native_subprocess_env_expands_persisted_alfredrc_home_pointer() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();

        let root = temp_root("alfred-persisted-alfredrc-home-pointer");
        let home = root.join("home");
        let runtime = root.join("runtime");
        let custom_rc = home.join("custom.alfredrc");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create runtime");
        std::fs::write(home.join(".alfredrc"), "ALFREDRC=~/custom.alfredrc\n")
            .expect("write home alfredrc pointer");
        std::fs::write(
            &custom_rc,
            format!("ALFRED_HOME='{}'\n", runtime.to_string_lossy()),
        )
        .expect("write custom alfredrc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFREDRC"),
            Some(&custom_rc.to_string_lossy().to_string())
        );
        assert_eq!(
            env.get("ALFRED_HOME"),
            Some(&runtime.to_string_lossy().to_string())
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
    }

    #[test]
    fn native_subprocess_env_preserves_explicit_overrides_over_alfredrc() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_alfred_bin = std::env::var("ALFRED_BIN").ok();
        let prev_gh = std::env::var("GH_BIN").ok();
        let prev_alfred_gh = std::env::var("ALFRED_GH_BIN").ok();

        let root = temp_root("alfred-explicit-env-overrides");
        let home = root.join("home");
        let stale_runtime = root.join("stale-runtime");
        let explicit_runtime = root.join("explicit-runtime");
        let stale_bin = root.join("stale").join("alfred");
        let explicit_bin = root.join("explicit").join("alfred");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&stale_runtime).expect("create stale runtime");
        fs::create_dir_all(&explicit_runtime).expect("create explicit runtime");
        touch(&stale_bin);
        touch(&explicit_bin);
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_BIN='{}'\nGH_BIN=/stale/bin/gh # stale\n",
                stale_runtime.to_string_lossy(),
                stale_bin.to_string_lossy()
            ),
        )
        .expect("write temp alfredrc");

        std::env::set_var("HOME", &home);
        std::env::set_var("ALFRED_HOME", &explicit_runtime);
        std::env::remove_var("ALFREDRC");
        std::env::set_var("ALFRED_BIN", &explicit_bin);
        std::env::set_var("GH_BIN", "/explicit/bin/gh");
        std::env::remove_var("ALFRED_GH_BIN");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFRED_HOME"),
            Some(&explicit_runtime.to_string_lossy().to_string())
        );
        assert_eq!(
            env.get("ALFRED_BIN"),
            Some(&explicit_bin.to_string_lossy().to_string())
        );
        assert_eq!(env.get("GH_BIN"), Some(&"/explicit/bin/gh".to_string()));
        assert_eq!(
            resolve_program("alfred"),
            explicit_bin.to_string_lossy().into_owned()
        );
        assert_eq!(resolve_gh_bin(), "/explicit/bin/gh");

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_BIN", prev_alfred_bin);
        restore_var("GH_BIN", prev_gh);
        restore_var("ALFRED_GH_BIN", prev_alfred_gh);
    }

    #[test]
    fn config_parser_preserves_quoted_hashes_when_stripping_comments() {
        assert_eq!(
            strip_inline_comment("\"value # literal\" # comment"),
            "\"value # literal\""
        );
        assert_eq!(
            strip_inline_comment("'value # literal' # comment"),
            "'value # literal'"
        );
        assert_eq!(
            decode_config_value(strip_inline_comment("\"value # literal\" # comment"), None),
            "value # literal"
        );
        assert_eq!(
            decode_config_value(strip_inline_comment("'value # literal' # comment"), None),
            "value # literal"
        );
        assert_eq!(
            decode_config_value(strip_inline_comment("prefix#literal # comment"), None),
            "prefix#literal"
        );
        assert_eq!(
            decode_config_value(strip_inline_comment("#abc"), None),
            "#abc"
        );
        assert_eq!(
            decode_config_value(strip_inline_comment(" # comment"), None),
            ""
        );
    }

    #[test]
    fn native_subprocess_env_strips_inline_comments_before_stop_controls() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-stop-control-comments");
        let home = root.join("home");
        fs::create_dir_all(&home).expect("create temp home");
        std::fs::write(
            home.join(".alfredrc"),
            "ALFRED_AUTO_PROMOTE=0 # opted out\nALFRED_AUTO_PROMOTE_KILL=1 # halt now\n",
        )
        .expect("write temp alfredrc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_AUTO_PROMOTE");
        std::env::remove_var("ALFRED_AUTO_PROMOTE_KILL");

        let env = merged_alfred_env();
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE_KILL"), Some(&"1".to_string()));

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn native_subprocess_env_file_stop_controls_override_enabling_process_values() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-file-stop-control-over-process");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create temp runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write temp alfredrc");
        std::fs::write(
            runtime.join(".env"),
            "ALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n",
        )
        .expect("write temp env");

        std::env::set_var("HOME", &home);
        std::env::set_var("ALFRED_HOME", &runtime);
        std::env::remove_var("ALFREDRC");
        std::env::set_var("ALFRED_AUTO_PROMOTE", "1");
        std::env::set_var("ALFRED_AUTO_PROMOTE_KILL", "0");

        let env = merged_alfred_env();
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE_KILL"), Some(&"1".to_string()));

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn native_subprocess_env_malformed_file_kill_overrides_enabling_values() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-malformed-kill-over-process");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create temp runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE_KILL=0\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write temp alfredrc");
        std::fs::write(runtime.join(".env"), "ALFRED_AUTO_PROMOTE_KILL=fales\n")
            .expect("write temp env");

        std::env::set_var("HOME", &home);
        std::env::set_var("ALFRED_HOME", &runtime);
        std::env::remove_var("ALFREDRC");
        std::env::set_var("ALFRED_AUTO_PROMOTE_KILL", "0");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFRED_AUTO_PROMOTE_KILL"),
            Some(&"fales".to_string())
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn native_subprocess_env_malformed_judge_overrides_enabling_values() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_judge = std::env::var("ALFRED_AUTO_PROMOTE_LLM_JUDGE").ok();

        let root = temp_root("alfred-malformed-judge-over-process");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create temp runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE_LLM_JUDGE=1\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write temp alfredrc");
        std::fs::write(runtime.join(".env"), "ALFRED_AUTO_PROMOTE_LLM_JUDGE=treu\n")
            .expect("write temp env");

        std::env::set_var("HOME", &home);
        std::env::set_var("ALFRED_HOME", &runtime);
        std::env::remove_var("ALFREDRC");
        std::env::set_var("ALFRED_AUTO_PROMOTE_LLM_JUDGE", "1");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFRED_AUTO_PROMOTE_LLM_JUDGE"),
            Some(&"treu".to_string())
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE_LLM_JUDGE", prev_judge);
    }

    #[test]
    fn native_subprocess_env_loads_alfredrc_before_runtime_env_file() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_workspace = std::env::var("WORKSPACE_ROOT").ok();

        let root = temp_root("alfred-child-env");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create temp runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=0\nWORKSPACE_ROOT=$HOME/work\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write temp alfredrc");
        std::fs::write(
            runtime.join(".env"),
            "ALFRED_AUTO_PROMOTE=1\nALFRED_CHILD_ONLY=from-env\n",
        )
        .expect("write temp runtime env");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_AUTO_PROMOTE");
        std::env::remove_var("WORKSPACE_ROOT");

        let env = merged_alfred_env();
        assert_eq!(
            env.get("ALFRED_HOME"),
            Some(&runtime.to_string_lossy().to_string())
        );
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_CHILD_ONLY"), Some(&"from-env".to_string()));
        assert_eq!(
            env.get("WORKSPACE_ROOT"),
            Some(&home.join("work").to_string_lossy().to_string())
        );

        let command = command_with_cli_path("alfred");
        let command_env: HashMap<String, String> = command
            .get_envs()
            .filter_map(|(key, value)| {
                Some((
                    key.to_string_lossy().into_owned(),
                    value?.to_string_lossy().into_owned(),
                ))
            })
            .collect();
        assert_eq!(
            command_env.get("ALFRED_AUTO_PROMOTE"),
            Some(&"0".to_string())
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("WORKSPACE_ROOT", prev_workspace);
    }

    #[test]
    fn native_subprocess_env_preserves_process_auto_promote_stop_control() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-child-stop-control");
        let home = root.join("home");
        fs::create_dir_all(&home).expect("create temp home");
        std::fs::write(
            home.join(".alfredrc"),
            "ALFRED_AUTO_PROMOTE=1\nALFRED_AUTO_PROMOTE_KILL=0\n",
        )
        .expect("write temp alfredrc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");
        std::env::set_var("ALFRED_AUTO_PROMOTE", "0");
        std::env::set_var("ALFRED_AUTO_PROMOTE_KILL", "1");

        let env = merged_alfred_env();
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE_KILL"), Some(&"1".to_string()));

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn native_subprocess_env_allows_runtime_env_stop_control_to_override_alfredrc() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_auto_promote = std::env::var("ALFRED_AUTO_PROMOTE").ok();
        let prev_auto_promote_kill = std::env::var("ALFRED_AUTO_PROMOTE_KILL").ok();

        let root = temp_root("alfred-child-env-stop-control");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(&runtime).expect("create temp runtime");
        std::fs::write(
            home.join(".alfredrc"),
            format!(
                "ALFRED_HOME='{}'\nALFRED_AUTO_PROMOTE=1\nALFRED_AUTO_PROMOTE_KILL=0\n",
                runtime.to_string_lossy()
            ),
        )
        .expect("write temp alfredrc");
        std::fs::write(
            runtime.join(".env"),
            "ALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n",
        )
        .expect("write temp runtime env");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_AUTO_PROMOTE");
        std::env::remove_var("ALFRED_AUTO_PROMOTE_KILL");

        let env = merged_alfred_env();
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE"), Some(&"0".to_string()));
        assert_eq!(env.get("ALFRED_AUTO_PROMOTE_KILL"), Some(&"1".to_string()));

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_AUTO_PROMOTE", prev_auto_promote);
        restore_var("ALFRED_AUTO_PROMOTE_KILL", prev_auto_promote_kill);
    }

    #[test]
    fn alfred_resolver_uses_configured_runtime_home_bin() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfred_bin = std::env::var("ALFRED_BIN").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_home = std::env::var("HOME").ok();

        let root = temp_root("alfred-home-bin");
        let runtime = root.join("runtime").join("bin").join("alfred");
        let local = root.join("user").join(".local").join("bin").join("alfred");
        touch(&local);
        touch(&runtime);

        std::env::set_var("ALFRED_HOME", root.join("runtime"));
        std::env::remove_var("ALFRED_BIN");
        std::env::remove_var("ALFREDRC");
        std::env::set_var("HOME", root.join("user"));
        assert_eq!(
            resolve_program("alfred"),
            runtime.to_string_lossy().into_owned()
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFRED_BIN", prev_alfred_bin);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("HOME", prev_home);
    }

    #[test]
    fn gh_resolver_prefers_configured_env_var() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred_gh = std::env::var("ALFRED_GH_BIN").ok();
        let prev_gh = std::env::var("GH_BIN").ok();

        std::env::set_var("ALFRED_GH_BIN", "/custom/bin/gh");
        std::env::set_var("GH_BIN", "/other/bin/gh");
        assert_eq!(resolve_gh_bin(), "/custom/bin/gh");

        restore_var("ALFRED_GH_BIN", prev_alfred_gh);
        restore_var("GH_BIN", prev_gh);
    }

    #[test]
    fn gh_resolver_reads_alfred_env_file() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();
        let prev_alfred_gh = std::env::var("ALFRED_GH_BIN").ok();
        let prev_gh = std::env::var("GH_BIN").ok();

        let dir = std::env::temp_dir().join(format!("alfred-gh-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("create temp home");
        std::fs::write(dir.join(".env"), "ALFRED_GH_BIN='/configured/gh'\n")
            .expect("write temp env");

        std::env::set_var("ALFRED_HOME", &dir);
        std::env::remove_var("ALFREDRC");
        std::env::remove_var("ALFRED_GH_BIN");
        std::env::remove_var("GH_BIN");
        assert_eq!(resolve_gh_bin(), "/configured/gh");

        let _ = std::fs::remove_dir_all(&dir);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
        restore_var("ALFRED_GH_BIN", prev_alfred_gh);
        restore_var("GH_BIN", prev_gh);
    }

    #[test]
    fn native_subprocess_path_includes_common_cli_dirs() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_path = std::env::var("PATH").ok();

        std::env::set_var("HOME", "/tmp/alfred-home");
        std::env::set_var("PATH", "/usr/bin:/bin");
        let path = augmented_cli_path().to_string_lossy().to_string();

        assert!(path.contains("/tmp/alfred-home/.local/bin"));
        assert!(path.contains("/tmp/alfred-home/.alfred/bin"));
        assert!(path.contains("/opt/homebrew/bin"));
        assert!(path.contains("/opt/homebrew/sbin"));
        assert!(path.contains("/usr/local/bin"));
        assert!(path.contains("/usr/local/sbin"));
        assert!(path.ends_with("/usr/bin:/bin"));

        restore_var("HOME", prev_home);
        restore_var("PATH", prev_path);
    }

    #[test]
    fn server_token_path_resolves_under_alfred_home_state_dir() {
        // Mutating the process environment is global, so this test owns these
        // vars for its duration and restores them afterward. It runs serially
        // with the other env-touching token test via a shared lock.
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();

        std::env::set_var("ALFRED_HOME", "/tmp/example-alfred-home");
        let path = server_token_path().expect("ALFRED_HOME resolves a token path");
        assert_eq!(
            path,
            PathBuf::from("/tmp/example-alfred-home/state/server-token")
        );

        restore_var("ALFRED_HOME", prev_alfred);
    }

    #[test]
    fn server_token_path_uses_alfredrc_runtime_home() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_home = std::env::var("HOME").ok();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();
        let prev_alfredrc = std::env::var("ALFREDRC").ok();

        let root = temp_root("alfred-token-alfredrc");
        let home = root.join("home");
        let runtime = root.join("runtime");
        fs::create_dir_all(&home).expect("create temp home");
        fs::create_dir_all(runtime.join("state")).expect("create temp runtime state");
        std::fs::write(
            home.join(".alfredrc"),
            format!("ALFRED_HOME='{}'\n", runtime.to_string_lossy()),
        )
        .expect("write temp alfredrc");

        std::env::set_var("HOME", &home);
        std::env::remove_var("ALFRED_HOME");
        std::env::remove_var("ALFREDRC");

        assert_eq!(
            server_token_path().expect("alfredrc ALFRED_HOME resolves a token path"),
            runtime.join("state").join("server-token")
        );

        let _ = std::fs::remove_dir_all(&root);
        restore_var("HOME", prev_home);
        restore_var("ALFRED_HOME", prev_alfred);
        restore_var("ALFREDRC", prev_alfredrc);
    }

    #[test]
    fn read_server_token_returns_token_written_under_state_dir() {
        let _guard = ENV_LOCK.lock().unwrap();
        let prev_alfred = std::env::var("ALFRED_HOME").ok();

        let dir = std::env::temp_dir().join(format!("alfred-token-test-{}", std::process::id()));
        let state = dir.join("state");
        std::fs::create_dir_all(&state).expect("create temp state dir");
        std::fs::write(state.join("server-token"), "  secret-token-value\n")
            .expect("write token file");

        std::env::set_var("ALFRED_HOME", &dir);
        assert_eq!(read_server_token().as_deref(), Some("secret-token-value"));

        // An empty token file is treated as absent.
        std::fs::write(state.join("server-token"), "   \n").expect("blank token file");
        assert_eq!(read_server_token(), None);

        let _ = std::fs::remove_dir_all(&dir);
        restore_var("ALFRED_HOME", prev_alfred);
    }

    fn restore_var(name: &str, prev: Option<String>) {
        match prev {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
    }

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
}
