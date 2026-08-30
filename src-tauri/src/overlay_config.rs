//! File-backed configuration owned by the desktop overlay.
//!
//! This lives beside glass-host's `cloud_sync.json` in the shared user-data
//! directory. The server will eventually expose it in the management UI, but
//! key handling stays native and deliberately does not depend on the server.

use serde::Deserialize;

#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(crate) enum FeedbackKey {
    #[default]
    LeftAlt,
    /// Experimental: the MTGA IL2CPP dump is inconclusive about whether Full
    /// Control ignores Right Ctrl. Keep this option available but unverified.
    RightCtrl,
    /// Legacy binding retained for users who prefer the former behavior.
    LeftCtrl,
}

impl FeedbackKey {
    #[cfg(target_os = "macos")]
    pub(crate) const fn config_value(self) -> &'static str {
        match self {
            Self::LeftAlt => "left_alt",
            Self::RightCtrl => "right_ctrl",
            Self::LeftCtrl => "left_ctrl",
        }
    }

    #[cfg(target_os = "windows")]
    pub(crate) const fn windows_label(self) -> &'static str {
        match self {
            Self::LeftAlt => "Left Alt",
            Self::RightCtrl => "Right Ctrl",
            Self::LeftCtrl => "Left Ctrl",
        }
    }
}

#[derive(Default, Deserialize)]
struct OverlayConfig {
    #[serde(default)]
    feedback_key: FeedbackKey,
}

fn parse(contents: &str) -> FeedbackKey {
    serde_json::from_str::<OverlayConfig>(contents)
        .map(|config| config.feedback_key)
        .unwrap_or_default()
}

/// Read `<user_data>/overlay_config.json`, defaulting safely when it is absent
/// or malformed. This mirrors cloud config's best-effort file convention.
pub(crate) fn load(app: &tauri::AppHandle) -> FeedbackKey {
    std::fs::read_to_string(crate::report::app_data_dir(app).join("overlay_config.json"))
        .ok()
        .map(|contents| parse(&contents))
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_when_missing_field_or_invalid() {
        assert_eq!(parse("{}"), FeedbackKey::LeftAlt);
        assert_eq!(parse("not json"), FeedbackKey::LeftAlt);
        assert_eq!(parse(r#"{"feedback_key":"unknown"}"#), FeedbackKey::LeftAlt);
    }

    #[test]
    fn parses_all_supported_keys() {
        assert_eq!(
            parse(r#"{"feedback_key":"left_alt"}"#),
            FeedbackKey::LeftAlt
        );
        assert_eq!(
            parse(r#"{"feedback_key":"right_ctrl"}"#),
            FeedbackKey::RightCtrl
        );
        assert_eq!(
            parse(r#"{"feedback_key":"left_ctrl"}"#),
            FeedbackKey::LeftCtrl
        );
    }
}
