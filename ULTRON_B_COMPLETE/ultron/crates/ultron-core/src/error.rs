use thiserror::Error;

#[derive(Debug, Error)]
pub enum CoreError {
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("toml: {0}")]
    Toml(#[from] toml::de::Error),
    #[error("toml-ser: {0}")]
    TomlSer(#[from] toml::ser::Error),
    #[error("json: {0}")]
    Json(#[from] serde_json::Error),
    #[error("quantum log: {0}")]
    QLog(#[from] ultron_quantum_log::QLogError),
    #[error("config: {0}")]
    Config(String),
    #[error("ws: {0}")]
    Ws(String),
    #[error("hook: {0}")]
    Hook(String),
    #[error("shutdown")]
    Shutdown,
    #[error("other: {0}")]
    Other(String),
}

pub type CoreResult<T> = std::result::Result<T, CoreError>;
