use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use anyhow::{anyhow, Result};
use goose_providers::model::ModelConfig;
use tracing::warn;

use crate::config::Config;
use crate::providers::base::Provider;
use crate::providers::provider_registry::ProviderEntry;
use crate::session::extension_data::EnabledExtensionsState;
use crate::session::Session;

type SessionSlot = Arc<tokio::sync::Mutex<Option<Arc<dyn Provider>>>>;
type SharedProviders = HashMap<String, (u64, Arc<dyn Provider>)>;

#[derive(Default)]
pub struct ProviderManager {
    shared: tokio::sync::Mutex<SharedProviders>,
    sessions: Mutex<HashMap<String, SessionSlot>>,
}

impl ProviderManager {
    pub async fn provider_for(&self, session: &Session) -> Result<Arc<dyn Provider>> {
        let name = provider_name_for(session)?;
        let slot = self.slot(&session.id);
        let mut slot = slot.lock().await;
        if let Some(provider) = slot.as_ref().filter(|p| p.get_name() == name) {
            return Ok(provider.clone());
        }

        // A session carrying a tenant API key runs on that key, never on the shared
        // global-config provider. This must come before the shared path below, or a
        // subagent on e.g. `anthropic` would silently use the deployment's own
        // credentials instead of the caller's.
        if let Some(credentials) = tenant_credentials(session, &name)? {
            let provider = crate::providers::create_with_api_key(
                &name,
                &credentials.api_key,
                credentials.host.as_deref(),
            )
            .map_err(|e| {
                anyhow!("Session has a tenant API key but provider '{name}' cannot use it: {e}")
            })?;
            *slot = Some(provider.clone());
            return Ok(provider);
        }

        let entry = crate::providers::get_from_registry(&name).await?;
        if !entry.session_bound() {
            let provider = self.shared(&name, &entry).await?;
            *slot = None;
            return Ok(provider);
        }

        let extensions = EnabledExtensionsState::extensions_or_default(
            Some(&session.extension_data),
            Config::global(),
        );
        let provider = entry
            .create_with_working_dir(extensions, session.working_dir.clone())
            .await?;
        let model_config = entry.normalize_model_config(model_config_for(session)?)?;
        if let Err(e) = provider.apply_model_selection(&model_config).await {
            warn!("Failed to apply model selection to provider: {e}");
        }
        provider
            .update_mode(&session.id, session.goose_mode)
            .await
            .map_err(|e| anyhow!("Failed to propagate mode to provider: {e}"))?;
        *slot = Some(provider.clone());
        Ok(provider)
    }

    /// The provider already serving the session, if any; never builds one.
    pub async fn pinned(&self, session: &Session) -> Option<Arc<dyn Provider>> {
        let name = provider_name_for(session).ok()?;
        self.slot(&session.id)
            .lock()
            .await
            .clone()
            .filter(|provider| provider.get_name() == name)
    }

    pub async fn set_provider(&self, session_id: &str, provider: Arc<dyn Provider>) {
        *self.slot(session_id).lock().await = Some(provider);
    }

    pub fn release(&self, session_id: &str) {
        self.sessions.lock().unwrap().remove(session_id);
    }

    fn slot(&self, session_id: &str) -> SessionSlot {
        self.sessions
            .lock()
            .unwrap()
            .entry(session_id.to_string())
            .or_default()
            .clone()
    }

    async fn shared(&self, name: &str, entry: &ProviderEntry) -> Result<Arc<dyn Provider>> {
        let generation = Config::global().generation();
        let mut shared = self.shared.lock().await;
        if let Some((cached_generation, provider)) = shared.get(name) {
            if *cached_generation == generation {
                return Ok(provider.clone());
            }
        }
        let provider = entry.create(Vec::new()).await?;
        shared.insert(name.to_string(), (generation, provider.clone()));
        Ok(provider)
    }
}

/// A tenant's own provider credentials, forwarded by CowGooseService.
pub(crate) struct TenantCredentials {
    pub(crate) api_key: String,
    pub(crate) host: Option<String>,
}

/// `gemini` is how the platform names the provider goose calls `google`.
fn canonical_provider(name: &str) -> String {
    match name.trim().to_ascii_lowercase().as_str() {
        "gemini" => "google".to_string(),
        other => other.to_string(),
    }
}

/// Read the session's tenant provider credentials: `x-api-key` plus an optional
/// host override. The key comes from the process-local secret store, because
/// `extension_data` no longer persists it (§14). `Ok(None)` is the normal
/// single-tenant case.
///
/// An error means the session has a key issued for a different provider than the
/// one now being built. A subagent can override its provider by name, and sending
/// one vendor's key to another vendor's API would leak it.
pub(crate) fn tenant_credentials(
    session: &Session,
    provider_name: &str,
) -> Result<Option<TenantCredentials>> {
    let Some(headers) =
        crate::session::session_secrets::merged_headers(&session.id, &session.extension_data)
    else {
        return Ok(None);
    };

    let mut api_key = None;
    let mut host = None;
    let mut issued_for = None;
    for (key, value) in &headers {
        let Some(value) = value.as_str() else {
            continue;
        };
        match key.to_lowercase().as_str() {
            "x-api-key" => api_key = Some(value.to_string()),
            "x-openai-host" | "x-anthropic-host" | "x-host" => host = Some(value.to_string()),
            "x-provider-name" => issued_for = Some(value.to_string()),
            _ => {}
        }
    }

    let Some(api_key) = api_key else {
        return Ok(None);
    };
    if let Some(issued_for) = &issued_for {
        if canonical_provider(issued_for) != canonical_provider(provider_name) {
            return Err(anyhow!(
                "Session's tenant API key was issued for provider '{issued_for}', \
                 not '{provider_name}'; refusing to send it to a different provider"
            ));
        }
    }
    Ok(Some(TenantCredentials { api_key, host }))
}

pub fn provider_name_for(session: &Session) -> Result<String> {
    match &session.provider_name {
        Some(name) => Ok(name.clone()),
        None => Config::global()
            .get_goose_provider()
            .map_err(|_| anyhow!("Could not configure agent: missing provider")),
    }
}

pub fn model_config_for(session: &Session) -> Result<ModelConfig> {
    if let Some(model_config) = &session.model_config {
        return Ok(model_config.clone());
    }
    let config = Config::global();
    let provider_name = provider_name_for(session)?;
    let model_name = config
        .get_goose_model()
        .map_err(|_| anyhow!("Could not resolve model config: missing model"))?;
    crate::model_config::model_config_from_user_config(&provider_name, &model_name)
        .map_err(|e| anyhow!("Could not resolve model config: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::GooseMode;
    use crate::session::extension_data::ExtensionData;
    use crate::session::{SessionManager, SessionType};
    use tempfile::TempDir;

    /// Removes a session's in-memory credentials when the test ends. Session ids
    /// are sequential per database, so a leaked entry would be inherited by an
    /// unrelated test's session with the same id.
    struct SecretsGuard(String);

    impl Drop for SecretsGuard {
        fn drop(&mut self) {
            crate::session::session_secrets::remove(&self.0);
        }
    }

    async fn session(
        dir: &TempDir,
        provider: &str,
        issued_for: Option<&str>,
        api_key: Option<&str>,
    ) -> (Session, SecretsGuard) {
        let manager = SessionManager::new(dir.path().to_path_buf());
        let session = manager
            .create_session(
                dir.path().to_path_buf(),
                "Tenant".to_string(),
                SessionType::User,
                GooseMode::Auto,
            )
            .await
            .unwrap();
        let guard = SecretsGuard(session.id.clone());

        let mut data = ExtensionData::default();
        if let Some(issued_for) = issued_for {
            data.extension_states.insert(
                "websocket_headers.v0".to_string(),
                serde_json::json!({"x-provider-name": issued_for}),
            );
        }
        manager
            .update(&session.id)
            .provider_name(provider)
            .model_config(goose_providers::model::ModelConfig::new("test-model"))
            .extension_data(data)
            .apply()
            .await
            .unwrap();
        if let Some(api_key) = api_key {
            let mut secrets = serde_json::Map::new();
            secrets.insert("x-api-key".to_string(), serde_json::json!(api_key));
            crate::session::session_secrets::store(&session.id, secrets);
        }
        (
            manager.get_session(&session.id, false).await.unwrap(),
            guard,
        )
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn a_tenant_key_builds_its_own_provider_never_the_shared_one() {
        let dir = TempDir::new().unwrap();
        let manager = ProviderManager::default();
        let (tenant, _tenant_guard) =
            session(&dir, "anthropic", Some("anthropic"), Some("sk-ant-tenant")).await;
        let (plain, _plain_guard) = session(&dir, "anthropic", None, None).await;

        let tenant_provider = manager
            .provider_for(&tenant)
            .await
            .expect("a tenant key must be enough to build the provider on its own");

        // Without global credentials the plain session cannot build a provider at
        // all, and the tenant path above was the only way to succeed. With them, the
        // two sessions must not share a provider.
        if let Ok(shared) = manager.provider_for(&plain).await {
            assert!(
                !Arc::ptr_eq(&tenant_provider, &shared),
                "a tenant-keyed session was handed the shared global-config provider"
            );
        }
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn a_tenant_key_is_never_sent_to_a_different_provider() {
        let dir = TempDir::new().unwrap();
        let manager = ProviderManager::default();
        // Issued for Anthropic, but the session (e.g. a delegate that overrode its
        // provider) now wants OpenAI.
        let (session, _guard) =
            session(&dir, "openai", Some("anthropic"), Some("sk-ant-tenant")).await;

        let error = manager
            .provider_for(&session)
            .await
            .err()
            .expect("the key must not be used against another vendor")
            .to_string();
        assert!(
            error.contains("refusing to send it to a different provider"),
            "{error}"
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn the_platform_name_gemini_matches_gooses_google_provider() {
        let dir = TempDir::new().unwrap();
        let (session, _guard) = session(&dir, "google", Some("gemini"), Some("AIza-tenant")).await;

        let credentials = tenant_credentials(&session, "google")
            .unwrap()
            .expect("the key was issued for this provider under its platform name");
        assert_eq!(credentials.api_key, "AIza-tenant");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn a_session_without_a_tenant_key_has_no_credentials() {
        let dir = TempDir::new().unwrap();
        let (session, _guard) = session(&dir, "anthropic", Some("anthropic"), None).await;
        assert!(tenant_credentials(&session, "anthropic").unwrap().is_none());
    }
}
