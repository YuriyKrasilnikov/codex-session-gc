use codex_state::SqliteConfig;
use codex_thread_store::{
    CleanupOutcome, CleanupStatus, ClosedWorkCleanupRequest, LocalThreadStore,
    LocalThreadStoreConfig,
};
use codex_utils_absolute_path::AbsolutePathBuf;
use sha2::{Digest, Sha256};
use std::io::{self, Read};

const MAX_REQUEST_BYTES: u64 = 16 * 1024 * 1024;

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let mut bytes = Vec::new();
    let result = io::stdin()
        .take(MAX_REQUEST_BYTES + 1)
        .read_to_end(&mut bytes);
    if bytes.len() <= usize::try_from(MAX_REQUEST_BYTES).unwrap_or(0)
        && let Ok(value) = serde_json::from_slice::<serde_json::Value>(&bytes)
    {
        if value.get("operation").is_some() {
            let reply = keep_request(value).await;
            let code = if reply["status"] == "confirmed" { 0 } else { 1 };
            println!("{}", reply);
            std::process::exit(code);
        }
    }
    let digest = Sha256::digest(&bytes).into();
    let outcome = if let Err(error) = result {
        refused(digest, error.to_string())
    } else {
        match u64::try_from(bytes.len()) {
            Err(error) => refused(digest, error.to_string()),
            Ok(length) if length > MAX_REQUEST_BYTES => {
                refused(digest, "request exceeds supported size".to_owned())
            }
            Ok(_) => match store_from_request(&bytes) {
                Ok(store) => store.cleanup_once(&bytes).await,
                Err(error) => refused(digest, error),
            },
        }
    };
    let code = match outcome.status {
        CleanupStatus::Confirmed => 0,
        CleanupStatus::Refused => 1,
        CleanupStatus::Partial => 2,
        CleanupStatus::Unknown => 3,
    };
    match serde_json::to_string(&outcome) {
        Ok(reply) => println!("{reply}"),
        Err(error) => {
            eprintln!("native outcome serialization failed: {error}");
            std::process::exit(3);
        }
    }
    std::process::exit(code);
}

fn refused(request_sha256: [u8; 32], detail: String) -> CleanupOutcome {
    CleanupOutcome {
        request_sha256,
        status: CleanupStatus::Refused,
        detail,
        removed_rollouts: Vec::new(),
        committed_databases: Vec::new(),
        preserved_name_index: None,
    }
}

fn store_from_request(bytes: &[u8]) -> Result<LocalThreadStore, String> {
    let request: ClosedWorkCleanupRequest =
        serde_json::from_slice(bytes).map_err(|error| error.to_string())?;
    let (codex_home, sqlite_home, provider) =
        (request.codex_home, request.sqlite_home, request.provider);
    let sqlite = SqliteConfig::from_sqlite_home(
        AbsolutePathBuf::try_from(sqlite_home).map_err(|error| error.to_string())?,
    );
    LocalThreadStore::new_for_bounded_cleanup(LocalThreadStoreConfig {
        codex_home,
        sqlite,
        default_model_provider_id: provider,
    })
    .map_err(|error| error.to_string())
}

async fn keep_request(value: serde_json::Value) -> serde_json::Value {
    let result: Result<_, String> = async {
        let object = value.as_object().ok_or("request must be an object")?;
        if object.keys().any(|key| {
            ![
                "operation",
                "codex_home",
                "sqlite_home",
                "thread_id",
                "home_device",
                "home_inode",
            ]
            .contains(&key.as_str())
        }) {
            return Err("unknown Keep request field".to_owned());
        }
        let operation = object
            .get("operation")
            .and_then(|v| v.as_str())
            .ok_or("operation required")?;
        let home = object
            .get("codex_home")
            .and_then(|v| v.as_str())
            .ok_or("codex_home required")?;
        let sqlite_home = object
            .get("sqlite_home")
            .and_then(|v| v.as_str())
            .ok_or("sqlite_home required")?;
        let id = match object.get("thread_id") {
            Some(v) => Some(v.as_str().ok_or("thread_id must be a string")?),
            None => None,
        };
        let home_device = object
            .get("home_device")
            .and_then(|v| v.as_u64())
            .ok_or("home_device required")?;
        let home_inode = object
            .get("home_inode")
            .and_then(|v| v.as_u64())
            .ok_or("home_inode required")?;
        let sqlite = SqliteConfig::from_sqlite_home(
            AbsolutePathBuf::try_from(std::path::PathBuf::from(sqlite_home))
                .map_err(|e| e.to_string())?,
        );
        let store = LocalThreadStore::new_for_bounded_cleanup(LocalThreadStoreConfig {
            codex_home: home.into(),
            sqlite,
            default_model_provider_id: "openai".to_owned(),
        })
        .map_err(|e| e.to_string())?;
        store
            .keep_once(operation, id, home_device, home_inode)
            .await
    }
    .await;
    result.unwrap_or_else(|detail| serde_json::json!({"status":"refused","detail":detail}))
}
