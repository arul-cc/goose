use anyhow::Result;
use futures::future::BoxFuture;
use goose_providers::api_client::TlsConfig;
use goose_providers::base::{ProviderDescriptor, ProviderMetadata};
use goose_providers::google::{GoogleProvider, GOOGLE_API_HOST};

use crate::config::{Config, ExtensionConfig};
use crate::providers::base::ProviderDef;

pub struct GoogleProviderDef;

impl ProviderDescriptor for GoogleProviderDef {
    fn metadata() -> ProviderMetadata {
        GoogleProvider::metadata().with_setup(
            crate::providers::catalog::ProviderSetupMetadata::api_key(
                crate::providers::catalog::ProviderSetupGroup::Default,
            )
            .with_docs_url("https://aistudio.google.com/apikey"),
        )
    }
}

impl ProviderDef for GoogleProviderDef {
    type Provider = GoogleProvider;

    fn from_env(
        _extensions: Vec<ExtensionConfig>,
        tls_config: Option<TlsConfig>,
    ) -> BoxFuture<'static, Result<Self::Provider>> {
        Box::pin(from_env(tls_config))
    }
}

/// Build a provider from a tenant's own API key instead of global config, for
/// per-session provider switching (§1-3). `GOOGLE_HOST` and the thinking budget
/// still come from config: only the credential is per-tenant.
pub fn from_api_key(api_key: &str, host_override: Option<&str>) -> Result<GoogleProvider> {
    let config = Config::global();
    let host = match host_override {
        Some(h) => h.to_string(),
        None => config
            .get_param("GOOGLE_HOST")
            .unwrap_or_else(|_| GOOGLE_API_HOST.to_string()),
    };

    GoogleProvider::new(
        host,
        api_key.to_string(),
        None,
        Some(crate::session_context::session_id_request_builder()),
        config.get_param("GEMINI25_THINKING_BUDGET").ok(),
    )
}

pub async fn from_env(tls_config: Option<TlsConfig>) -> Result<GoogleProvider> {
    let config = Config::global();
    let api_key: String = config.get_secret("GOOGLE_API_KEY")?;
    let host: String = config
        .get_param("GOOGLE_HOST")
        .unwrap_or_else(|_| GOOGLE_API_HOST.to_string());

    let thinking_budget = config.get_param("GEMINI25_THINKING_BUDGET").ok();

    GoogleProvider::new(
        host,
        api_key,
        tls_config,
        Some(crate::session_context::session_id_request_builder()),
        thinking_budget,
    )
}
