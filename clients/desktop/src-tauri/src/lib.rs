use std::process::{Command, Stdio};
use std::time::Duration;

use reqwest::{Method, Url};
use serde::Serialize;

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
    request_alfred_json(base_url, path, Method::GET).await
}

#[tauri::command]
async fn post_alfred_json(base_url: String, path: String) -> Result<String, String> {
    request_alfred_json(base_url, path, Method::POST).await
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

async fn request_alfred_json(
    base_url: String,
    path: String,
    method: Method,
) -> Result<String, String> {
    let mut url = validate_base_url(&base_url)?;
    let (path_part, query) = validate_api_path(&path, &method)?;

    url.set_path(&path_part);
    url.set_query(query);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .user_agent("Alfred Desktop/0.1")
        .build()
        .map_err(|err| format!("could not prepare local request: {err}"))?;

    let response = client
        .request(method, url)
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
        is_allowed_followup_action(path_part)
    } else {
        false
    };
    if !allowed {
        return Err("API path is not part of the desktop contract".to_string());
    }

    Ok((path_part.to_string(), query))
}

fn is_allowed_read_path(path: &str) -> bool {
    let allowed = ["/api/status", "/api/actions", "/api/firings", "/api/plans"];
    allowed
        .iter()
        .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
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
    if !clean
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-'))
    {
        return Err("agent codename contains unsupported characters".to_string());
    }
    Ok(clean.to_string())
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
            start_alfred_runtime
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
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
}
