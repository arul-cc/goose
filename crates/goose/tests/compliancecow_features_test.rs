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
use goose::config::base::CONFIG_YAML_NAME;
use goose::config::paths::Paths;
use goose_test_support::EnforceSessionId;
use serial_test::serial;
use std::collections::HashMap;
use std::sync::Mutex;
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

/// Records the headers of every HTTP request reaching the mock MCP server.
#[derive(Clone, Default)]
struct HeaderLog(Arc<Mutex<Vec<HashMap<String, String>>>>);

impl HeaderLog {
    fn requests(&self) -> Vec<HashMap<String, String>> {
        self.0.lock().unwrap().clone()
    }
}

async fn record_headers(
    axum::extract::State(log): axum::extract::State<HeaderLog>,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let captured = request
        .headers()
        .iter()
        .filter_map(|(name, value)| {
            value
                .to_str()
                .ok()
                .map(|value| (name.as_str().to_lowercase(), value.to_string()))
        })
        .collect();
    log.0.lock().unwrap().push(captured);
    next.run(request).await
}

/// A real streamable-HTTP MCP server (same one the other fixtures use) with a
/// middleware that records the headers goose sends it.
async fn spawn_recording_mcp(log: HeaderLog) -> String {
    use rmcp::transport::streamable_http_server::{
        session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
    };

    let service = StreamableHttpService::new(
        || Ok::<_, std::io::Error>(goose_test_support::mcp::McpFixtureServer::new()),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default(),
    );
    let router = axum::Router::new()
        .nest_service("/mcp", service)
        .layer(axum::middleware::from_fn_with_state(log, record_headers));

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    format!("http://{addr}/mcp")
}

/// §4 forwarding side: headers named in the extension's `allowed_headers` must
/// reach the MCP server, and anything else in `websocket_headers.v0` must not.
/// This is the half that talks to cow-mcp in production.
#[test]
#[serial]
fn allow_listed_session_headers_are_forwarded_to_the_mcp_server() {
    let root_path = TEST_ROOT.path().to_string_lossy().to_string();
    let _env = env_lock::lock_env([
        ("GOOSE_PATH_ROOT", Some(root_path.as_str())),
        ("GOOSE_DISABLE_KEYRING", Some("1")),
    ]);

    run_test(async move {
        let log = HeaderLog::default();
        let mcp_url = spawn_recording_mcp(log.clone()).await;

        // data_root must be Paths::data_dir(): DynamicHeaderClient reads the
        // session through the process-wide SessionManager::instance(), which is
        // what `goose serve` also does.
        let data_root = Paths::data_dir();
        std::fs::create_dir_all(&data_root).unwrap();
        std::fs::write(
            data_root.join(CONFIG_YAML_NAME),
            format!(
                "GOOSE_MODEL: gpt-4o\nGOOSE_PROVIDER: openai\nGOOSE_DISABLE_KEYRING: true\n\
                 extensions:\n  mcp-fixture:\n    enabled: true\n    type: streamable_http\n\
                 \x20   name: mcp-fixture\n    description: MCP fixture\n    uri: \"{mcp_url}\"\n\
                 \x20   timeout: 30\n    allowed_headers:\n      - x-api-key\n      - x-cow-security-context\n"
            ),
        )
        .unwrap();

        let openai = common_tests::fixtures::OpenAiFixture::new(
            vec![],
            Arc::new(EnforceSessionId::default()),
        )
        .await;
        let mut conn = AcpServerConnection::new(
            TestConnectionConfig {
                data_root: data_root.clone(),
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
                        "x-cow-security-context": security_context("user-7"),
                        "x-not-allow-listed": "must-not-be-forwarded",
                    }
                }
            }),
        )
        .await
        .expect("extension_data/set should succeed");

        // Reloading the session rebuilds the MCP client, so the next handshake
        // is issued after the headers were planted.
        conn.load_session(&sid, Vec::new())
            .await
            .expect("load session");

        let requests = log.requests();
        assert!(
            !requests.is_empty(),
            "the mock MCP server should have received requests"
        );
        let forwarded = requests
            .iter()
            .find(|headers| headers.contains_key("x-api-key"))
            .unwrap_or_else(|| {
                panic!(
                    "no MCP request carried the allow-listed x-api-key; saw {} requests: {:?}",
                    requests.len(),
                    requests
                )
            });

        assert_eq!(
            forwarded.get("x-api-key").map(String::as_str),
            Some("tenant-key-1"),
            "allow-listed header value must match the session state"
        );
        assert!(
            forwarded.contains_key("x-cow-security-context"),
            "second allow-listed header must be forwarded too"
        );
        assert!(
            requests
                .iter()
                .all(|headers| !headers.contains_key("x-not-allow-listed")),
            "a header absent from allowed_headers must never be forwarded"
        );
    });
}
