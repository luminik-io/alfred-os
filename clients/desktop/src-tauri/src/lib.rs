use std::time::Duration;

use reqwest::Url;

#[tauri::command]
async fn fetch_alfred_json(base_url: String, path: String) -> Result<String, String> {
    let mut url = validate_base_url(&base_url)?;
    let (path_part, query) = validate_api_path(&path)?;

    url.set_path(&path_part);
    url.set_query(query);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .user_agent("Alfred Desktop/0.1")
        .build()
        .map_err(|err| format!("could not prepare local request: {err}"))?;

    let response = client
        .get(url)
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

fn validate_api_path(path: &str) -> Result<(String, Option<&str>), String> {
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

    let allowed = ["/api/status", "/api/actions", "/api/firings", "/api/plans"];
    if !allowed.iter().any(|prefix| {
        trimmed == *prefix
            || trimmed.starts_with(&format!("{prefix}?"))
            || trimmed.starts_with(&format!("{prefix}/"))
    }) {
        return Err("API path is not part of the desktop contract".to_string());
    }

    let (path_part, query) = trimmed
        .split_once('?')
        .map_or((trimmed, None), |(path_part, query)| {
            (path_part, Some(query))
        });
    Ok((path_part.to_string(), query))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![fetch_alfred_json])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
