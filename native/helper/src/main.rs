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
