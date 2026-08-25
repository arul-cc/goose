//! Verification for the ComplianceCow fork features (see COMPLIANCECOW_FORK_FEATURES.md).
//!
//! These exercise the custom ACP methods and the multi-tenant plumbing that the
//! fork adds on top of upstream goose, so a future upstream sync that silently
//! drops one of them fails here instead of in production.
#![recursion_limit = "256"]
#[allow(dead_code)]
#[path = "acp_common_tests/mod.rs"]
mod common_tests;

use common_tests::fixtures::server::AcpServerConnection;
use common_tests::fixtures::{run_test, send_custom, Connection, TestConnectionConfig};
use goose::config::paths::Paths;
use goose_test_support::EnforceSessionId;
use serial_test::serial;
use std::sync::{Arc, LazyLock};

const SET_EXTENSION_DATA: &str = "_goose/unstable/session/extension_data/set";
const UPDATE_SESSION_PROVIDER: &str = "_goose/unstable/session/provider/update";

/// One root for the whole file: goose's session store is a process-wide static,
/// so per-test roots would make later tests look at a different database.
static TEST_ROOT: LazyLock<tempfile::TempDir> = LazyLock::new(|| tempfile::tempdir().unwrap());

/// The security context CowGooseService forwards; `ID` becomes Anthropic's
/// `metadata.user_id`.
fn security_context(user_id: &str) -> String {
    serde_json::json!({ "ID": user_id, "Org": "acme" }).to_string()
}

/// §4 injection side: `websocket_headers.v0` must land in the session's
/// extension_data, and a second call must MERGE rather than clobber — that is
/// the token-rotation path CowGooseService relies on.
#[test]
#[serial]
fn set_session_extension_data_persists_and_merges_websocket_headers() {
    let root_path = TEST_ROOT.path().to_string_lossy().to_string();
    let _env = env_lock::lock_env([
        ("GOOSE_PATH_ROOT", Some(root_path.as_str())),
        ("GOOSE_DISABLE_KEYRING", Some("1")),
    ]);

    run_test(async move {
        let openai = common_tests::fixtures::OpenAiFixture::new(
            vec![],
            Arc::new(EnforceSessionId::default()),
        )
        .await;
        let mut conn = AcpServerConnection::new(
            TestConnectionConfig {
                data_root: Paths::data_dir(),
                ..Default::default()
            },
            openai,
        )
        .await;

        let session = conn.new_session().await.expect("new session");
        let sid = {
            use common_tests::fixtures::Session as _;
            session.session.session_id().0.to_string()
        };

        send_custom(
            conn.cx(),
            SET_EXTENSION_DATA,
            serde_json::json!({
                "sessionId": sid,
                "extensionData": {
                    "websocket_headers.v0": {
                        "x-api-key": "tenant-key-1",
                        "x-cow-security-context": security_context("user-1"),
                    }
                }
            }),
        )
        .await
        .expect("extension_data/set should succeed");

        // A second, unrelated key must not wipe the first.
        send_custom(
            conn.cx(),
            SET_EXTENSION_DATA,
            serde_json::json!({
                "sessionId": sid,
                "extensionData": { "cow_tenant.v0": { "tenant": "acme" } }
            }),
        )
        .await
        .expect("second extension_data/set should succeed");

        let stored = goose::session::SessionManager::instance()
            .get_session(&sid, false)
            .await
            .expect("session should load");

        let headers = stored
            .extension_data
            .get_extension_state("websocket_headers", "v0")
            .expect("websocket_headers.v0 must survive the second set (merge, not clobber)");
        assert_eq!(
            headers.get("x-api-key").and_then(|v| v.as_str()),
            Some("tenant-key-1")
        );
        assert!(
            stored
                .extension_data
                .extension_states
                .contains_key("cow_tenant.v0"),
            "second key should also be present"
        );
    });
}

/// §1-3 + §6: the per-session provider method must accept an explicit tenant
/// api_key/host, merge request_params (§9 thinking control), and — for the
/// anthropic provider — attach `metadata.user_id` from the security context.
#[test]
#[serial]
fn update_session_provider_applies_tenant_key_request_params_and_user_id() {
    let root_path = TEST_ROOT.path().to_string_lossy().to_string();
    let _env = env_lock::lock_env([
        ("GOOSE_PATH_ROOT", Some(root_path.as_str())),
        ("GOOSE_DISABLE_KEYRING", Some("1")),
    ]);

    run_test(async move {
        let openai = common_tests::fixtures::OpenAiFixture::new(
            vec![],
            Arc::new(EnforceSessionId::default()),
        )
        .await;
        let mut conn = AcpServerConnection::new(
            TestConnectionConfig {
                data_root: Paths::data_dir(),
                ..Default::default()
            },
            openai,
        )
        .await;

        let session = conn.new_session().await.expect("new session");
        let sid = {
            use common_tests::fixtures::Session as _;
            session.session.session_id().0.to_string()
        };

        // Plant the tenant security context first so user_id can be derived.
        send_custom(
            conn.cx(),
            SET_EXTENSION_DATA,
            serde_json::json!({
                "sessionId": sid,
                "extensionData": {
                    "websocket_headers.v0": {
                        "x-cow-security-context": security_context("user-42"),
                    }
                }
            }),
        )
        .await
        .expect("extension_data/set should succeed");

        send_custom(
            conn.cx(),
            UPDATE_SESSION_PROVIDER,
            serde_json::json!({
                "sessionId": sid,
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "apiKey": "tenant-anthropic-key",
                "host": "https://api.deepseek.com/anthropic",
                "requestParams": { "thinking": { "type": "disabled" } }
            }),
        )
        .await
        .expect("session/provider/update should succeed with an explicit api key");

        let stored = goose::session::SessionManager::instance()
            .get_session(&sid, false)
            .await
            .expect("session should load");

        assert_eq!(
            stored.provider_name.as_deref(),
            Some("anthropic"),
            "provider must switch to the requested one"
        );

        let model_config = stored.model_config.expect("model_config must be persisted");
        let params = model_config
            .request_params
            .expect("request_params must be persisted");

        assert_eq!(
            params.get("thinking"),
            Some(&serde_json::json!({ "type": "disabled" })),
            "§9: explicit thinking directive must survive"
        );
        assert_eq!(
            params.get("metadata"),
            Some(&serde_json::json!({ "user_id": "user-42" })),
            "§6: metadata.user_id must be derived from x-cow-security-context"
        );
    });
}
