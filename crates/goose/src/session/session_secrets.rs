//! Process-local storage for the secret entries of a session's forwarded headers.
//!
//! `websocket_headers.v0` carries three tenant credentials: the LLM provider key
//! (`x-api-key`), the platform auth token (`authorization`) and the security
//! context (`x-cow-security-context`, which embeds another token). They used to
//! be serialized into `sessions.extension_data`, a plain TEXT column, which put
//! live credentials on disk and carried them into every session export, copy and
//! info response.
//!
//! They live here instead: keyed by session id, never serialized, gone when the
//! process exits. The host service re-pushes headers on every (re)connect, so a
//! restart repopulates this from the vault rather than from disk.
//!
//! The key is a session id, which `create_session` allocates as `YYYYMMDD_N`
//! counting up within one database. That is unique for a running server, which
//! opens one database — but not across two `SessionManager`s, so anything that
//! builds a second one (tests, tooling) must `remove` what it stores or it will
//! hand those credentials to the next session that lands on the same id.

use serde_json::{Map, Value};
use std::collections::HashMap;
use std::sync::{LazyLock, RwLock};

/// Header names whose values are credentials. Compared lowercased; the host
/// service already lowercases on the way in, but session imports and older rows
/// may not have.
const SECRET_HEADER_NAMES: &[&str] = &["authorization", "x-api-key", "x-cow-security-context"];

pub const WEBSOCKET_HEADERS_KEY: &str = "websocket_headers.v0";

static SESSION_SECRETS: LazyLock<RwLock<HashMap<String, Map<String, Value>>>> =
    LazyLock::new(|| RwLock::new(HashMap::new()));

pub fn is_secret_header(name: &str) -> bool {
    let lower = name.to_lowercase();
    SECRET_HEADER_NAMES.iter().any(|secret| *secret == lower)
}

/// Move the credential entries out of a `websocket_headers.v0` object, returning
/// them. What remains in `headers` is safe to persist.
pub fn split_secret_headers(headers: &mut Map<String, Value>) -> Map<String, Value> {
    let secret_keys: Vec<String> = headers
        .keys()
        .filter(|key| is_secret_header(key))
        .cloned()
        .collect();

    let mut secrets = Map::new();
    for key in secret_keys {
        if let Some(value) = headers.remove(&key) {
            secrets.insert(key, value);
        }
    }
    secrets
}

/// Merge `secrets` into the session's store. Merging rather than replacing lets
/// the host refresh one header without resending the rest.
pub fn store(session_id: &str, secrets: Map<String, Value>) {
    if secrets.is_empty() {
        return;
    }
    let mut guard = SESSION_SECRETS.write().unwrap_or_else(|e| e.into_inner());
    let entry = guard.entry(session_id.to_string()).or_default();
    for (key, value) in secrets {
        entry.insert(key, value);
    }
}

pub fn get(session_id: &str) -> Option<Map<String, Value>> {
    let guard = SESSION_SECRETS.read().unwrap_or_else(|e| e.into_inner());
    guard.get(session_id).cloned()
}

pub fn remove(session_id: &str) {
    let mut guard = SESSION_SECRETS.write().unwrap_or_else(|e| e.into_inner());
    guard.remove(session_id);
}

/// Copy one session's secrets onto another. Used when a subagent session
/// inherits its parent's tenant identity.
pub fn copy(from_session_id: &str, to_session_id: &str) {
    if let Some(secrets) = get(from_session_id) {
        store(to_session_id, secrets);
    }
}

/// Strip credential entries from a session's persisted header set. New sessions
/// never store them, but rows written before that change still carry them, and
/// export hands the whole row to the caller.
pub fn redact(extension_data: &mut crate::session::ExtensionData) {
    if let Some(value) = extension_data
        .extension_states
        .get_mut(WEBSOCKET_HEADERS_KEY)
    {
        if let Some(headers) = value.as_object_mut() {
            split_secret_headers(headers);
        }
    }
}

/// The session's full forwarded header set: what was persisted, with the
/// in-memory credentials layered back on top. Callers that forward or
/// authenticate must use this rather than reading `extension_data` directly,
/// which by design no longer holds the credentials.
pub fn merged_headers(
    session_id: &str,
    extension_data: &crate::session::ExtensionData,
) -> Option<Map<String, Value>> {
    let persisted = extension_data
        .get_extension_state("websocket_headers", "v0")
        .and_then(|value| value.as_object().cloned());
    let secrets = get(session_id);

    match (persisted, secrets) {
        (None, None) => None,
        (Some(headers), None) => Some(headers),
        (None, Some(secrets)) => Some(secrets),
        (Some(mut headers), Some(secrets)) => {
            headers.extend(secrets);
            Some(headers)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn headers(pairs: &[(&str, &str)]) -> Map<String, Value> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), json!(v)))
            .collect()
    }

    #[test]
    fn split_removes_every_credential_and_keeps_the_rest() {
        let mut h = headers(&[
            ("authorization", "tok"),
            ("x-api-key", "sk-tenant"),
            ("x-cow-security-context", "{\"AuthToken\":\"t\"}"),
            ("x-domain-name", "acme"),
            ("x-correlation-id", "abc"),
        ]);

        let secrets = split_secret_headers(&mut h);

        assert_eq!(secrets.len(), 3, "all three credentials must be extracted");
        assert!(secrets.contains_key("x-api-key"));
        assert!(secrets.contains_key("authorization"));
        assert!(secrets.contains_key("x-cow-security-context"));

        // What is left is what gets written to the database.
        assert_eq!(h.len(), 2);
        assert!(h.contains_key("x-domain-name"));
        let persisted = serde_json::to_string(&h).unwrap();
        for leaked in ["tok", "sk-tenant", "AuthToken"] {
            assert!(
                !persisted.contains(leaked),
                "credential {leaked} survived into the persisted header set: {persisted}"
            );
        }
    }

    #[test]
    fn split_matches_header_names_case_insensitively() {
        let mut h = headers(&[("Authorization", "tok"), ("X-Api-Key", "sk-tenant")]);
        let secrets = split_secret_headers(&mut h);
        assert_eq!(secrets.len(), 2, "canonical-cased names must still split");
        assert!(h.is_empty());
    }

    #[test]
    fn merged_headers_layers_secrets_over_persisted() {
        let session_id = "merge-test-session";
        let mut extension_data = crate::session::ExtensionData::new();
        extension_data.set_extension_state(
            "websocket_headers",
            "v0",
            json!({"x-domain-name": "acme"}),
        );
        store(session_id, headers(&[("x-api-key", "sk-tenant")]));

        let merged = merged_headers(session_id, &extension_data).expect("headers must resolve");

        assert_eq!(merged.get("x-domain-name").unwrap(), &json!("acme"));
        assert_eq!(merged.get("x-api-key").unwrap(), &json!("sk-tenant"));
        remove(session_id);
    }

    #[test]
    fn store_merges_rather_than_replacing() {
        let session_id = "merge-store-session";
        store(session_id, headers(&[("x-api-key", "sk-one")]));
        store(session_id, headers(&[("authorization", "tok")]));

        let got = get(session_id).expect("secrets must be stored");
        assert_eq!(
            got.len(),
            2,
            "a partial refresh must not drop other headers"
        );
        remove(session_id);
    }

    #[test]
    fn redact_strips_credentials_left_by_older_rows() {
        let mut extension_data = crate::session::ExtensionData::new();
        extension_data.set_extension_state(
            "websocket_headers",
            "v0",
            json!({"x-api-key": "sk-legacy", "authorization": "tok", "x-domain-name": "acme"}),
        );

        redact(&mut extension_data);

        let serialized = serde_json::to_string(&extension_data).unwrap();
        assert!(
            !serialized.contains("sk-legacy") && !serialized.contains("tok"),
            "a legacy row must not export its credentials: {serialized}"
        );
        assert!(
            serialized.contains("acme"),
            "non-secret headers must survive"
        );
    }

    #[test]
    fn removing_a_session_drops_its_credentials() {
        let session_id = "removal-test-session";
        store(session_id, headers(&[("x-api-key", "sk-tenant")]));
        remove(session_id);
        assert!(get(session_id).is_none());
    }
}
