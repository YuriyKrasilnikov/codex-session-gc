//! Native storage producers used by isolated behavioral fixtures.

use chrono::{DateTime, Utc};
use codex_protocol::models::{ContentItem, ResponseItem};
use codex_protocol::protocol::{
    SessionMeta, SessionMetaLine, SessionSource, SubAgentSource, ThreadHistoryMode,
};
use codex_protocol::{SessionId, ThreadId};
use codex_rollout::{
    RolloutConfig, RolloutItem, RolloutLine, RolloutRecorder, RolloutRecorderParams,
    WriterLockCoordinator,
};
use codex_state::{
    Phase2JobClaimOutcome, SqliteConfig, Stage1JobClaimOutcome, StateRuntime, ThreadGoal,
    ThreadGoalStatus, ThreadMetadataBuilder,
};
use codex_thread_store::{
    ArchiveThreadParams, LocalThreadStore, LocalThreadStoreConfig, ResumeThreadParams,
    ThreadMetadataPatch, ThreadPersistenceMetadata, ThreadStore, UpdateThreadMetadataParams,
};
use codex_utils_absolute_path::AbsolutePathBuf;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> anyhow::Result<()> {
    let arguments: Vec<_> = std::env::args().collect();
    let mode = arguments
        .get(1)
        .ok_or_else(|| anyhow::anyhow!("fixture mode required"))?;
    let home = PathBuf::from(
        arguments
            .get(2)
            .ok_or_else(|| anyhow::anyhow!("fixture home required"))?,
    );
    anyhow::ensure!(home.is_absolute(), "fixture home must be absolute");
    match mode.as_str() {
        "create-batch" => create(&home, false, true, true).await?,
        "compress-stock" => {
            let source = PathBuf::from(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("source rollout path required"))?,
            );
            stock_compression_fixture(&home, &source).await?;
        }
        "create-paginated" | "create-reverted" | "fork-peer" | "delete-native" | "read-lineage" => {
            publication_fixture(&home, mode, arguments.get(3)).await?;
        }
        "initialize-memory-v2" => {
            let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.clone())?);
            let state = StateRuntime::init(sqlite, "openai".to_owned()).await?;
            state
                .memories_for_version(codex_protocol::MemoryVersion::V2)
                .await?;
            state.close().await;
            println!("initialized");
        }
        "sample-history" => {
            let path = PathBuf::from(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("rollout path required"))?,
            );
            RolloutRecorder::load_rollout_items(&path).await?;
            println!("loaded");
        }
        "board-handle" => {
            let id = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("root id required"))?,
            )?;
            board_peer(&home, id).await?;
        }
        "supersede-memory" | "complete-memory" => {
            let id = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("thread id required"))?,
            )?;
            let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.clone())?);
            let state = StateRuntime::init(sqlite, "openai".to_owned()).await?;
            let old = DateTime::parse_from_rfc3339("2026-06-01T00:00:00Z")?.timestamp();
            if mode == "supersede-memory" {
                let Stage1JobClaimOutcome::Claimed {
                    ownership_token: first,
                } = state
                    .memories()
                    .try_claim_stage1_job(id, id, old, 0, 10)
                    .await?
                else {
                    anyhow::bail!("first claim unavailable")
                };
                let Stage1JobClaimOutcome::Claimed {
                    ownership_token: second,
                } = state
                    .memories()
                    .try_claim_stage1_job(id, id, old, 60, 10)
                    .await?
                else {
                    anyhow::bail!("superseding claim unavailable")
                };
                anyhow::ensure!(first != second, "claim token did not change");
                anyhow::ensure!(
                    state
                        .memories()
                        .mark_stage1_job_failed(id, &second, "fixture derived sample failure", 0)
                        .await?,
                    "mark failure unavailable"
                );
                println!(
                    "{}",
                    serde_json::json!({"old_token":first,"superseding_token":second})
                );
            } else {
                let token = arguments
                    .get(4)
                    .ok_or_else(|| anyhow::anyhow!("ownership token required"))?;
                let applied = state
                    .memories()
                    .mark_stage1_job_succeeded(id, token, old, "late output", "late summary", None)
                    .await?;
                println!("{}", serde_json::json!({"applied":applied}));
            }
            state.close().await;
        }
        "resume" => {
            let id = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("thread id required"))?,
            )?;
            let path = PathBuf::from(
                arguments
                    .get(4)
                    .ok_or_else(|| anyhow::anyhow!("rollout path required"))?,
            );
            let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.clone())?);
            let store = LocalThreadStore::new_for_bounded_cleanup(LocalThreadStoreConfig {
                codex_home: home.clone(),
                sqlite,
                default_model_provider_id: "openai".to_owned(),
            })?;
            store
                .resume_thread(ResumeThreadParams {
                    thread_id: id,
                    rollout_path: Some(path),
                    history: None,
                    history_revision: None,
                    include_archived: false,
                    metadata: ThreadPersistenceMetadata {
                        cwd: Some(home),
                        model_provider: "openai".to_owned(),
                        memory_mode: codex_protocol::protocol::ThreadMemoryMode::Disabled,
                    },
                })
                .await?;
            store.shutdown_thread(id).await?;
            println!("resumed");
        }
        "add-member" => {
            let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.clone())?);
            let state = StateRuntime::init(sqlite, "openai".to_owned()).await?;
            let id = ThreadId::default();
            let time = Utc::now();
            let source =
                SessionSource::SubAgent(SubAgentSource::Other("fixture-member".to_owned()));
            let path = write_history(&home, id, time, source.clone())?;
            state
                .upsert_thread(
                    &ThreadMetadataBuilder::new(id, path.clone(), time, source).build("openai"),
                )
                .await?;
            state.close().await;
            println!("{}", serde_json::json!({"id":id,"path":path}));
        }
        "hold-guard" | "try-guard" => {
            let id = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("thread id required"))?,
            )?;
            let coordinator = Arc::new(WriterLockCoordinator::new_for_bounded_cleanup(&home));
            let _guard = coordinator.acquire(id)?;
            println!("ready");
            io::stdout().flush()?;
            if mode == "hold-guard" {
                let mut line = String::new();
                io::stdin().read_line(&mut line)?;
            }
        }
        "create" | "create-recent" | "create-isolated" | "create-isolated-recent" => {
            create(
                &home,
                mode.ends_with("recent"),
                mode.starts_with("create-isolated"),
                false,
            )
            .await?
        }
        "add-pending" | "add-old-pending" | "add-goal" | "add-deferral" | "add-active-goal"
        | "add-running-job" | "add-expired-job" | "add-done-job" | "add-output" => {
            let target = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("thread id required"))?,
            )?;
            add_work(&home, target, mode).await?;
        }
        "select-memory" => {
            let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.clone())?);
            let state = StateRuntime::init(sqlite, "openai".to_owned()).await?;
            let target = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("thread id required"))?,
            )?;
            let outputs = state.memories().list_stage1_outputs_for_global(100).await?;
            let selected: Vec<_> = outputs
                .into_iter()
                .filter(|output| output.thread_id == target)
                .collect();
            anyhow::ensure!(selected.len() == 1, "native stage1 output absent");
            let Phase2JobClaimOutcome::Claimed {
                ownership_token,
                input_watermark,
            } = state
                .memories()
                .try_claim_global_phase2_job(target, 60)
                .await?
            else {
                anyhow::bail!("native phase2 claim unavailable")
            };
            anyhow::ensure!(
                state
                    .memories()
                    .mark_global_phase2_job_succeeded(&ownership_token, input_watermark, &selected)
                    .await?,
                "native phase2 completion failed"
            );
            state.close().await;
        }
        "hot-journal" => {
            let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.clone())?);
            let pool = sqlite.open_read_write_pool(&sqlite.logs_db_path()).await?;
            let mut connection = pool.acquire().await?.detach();
            pool.close().await;
            sqlx::query("PRAGMA journal_mode=DELETE")
                .execute(&mut connection)
                .await?;
            sqlx::query("PRAGMA cache_size=1")
                .execute(&mut connection)
                .await?;
            sqlx::query("BEGIN IMMEDIATE")
                .execute(&mut connection)
                .await?;
            for _ in 0..500 {
                sqlx::query("INSERT INTO logs(ts,ts_nanos,level,target,feedback_log_body) VALUES(0,0,'INFO','fixture',?)")
                    .bind("interrupted transaction ".repeat(200)).execute(&mut connection).await?;
            }
            std::process::exit(0);
        }
        "writer" | "metadata" => {
            let id = ThreadId::from_string(
                arguments
                    .get(3)
                    .ok_or_else(|| anyhow::anyhow!("thread id required"))?,
            )?;
            let path = PathBuf::from(
                arguments
                    .get(4)
                    .ok_or_else(|| anyhow::anyhow!("rollout path required"))?,
            );
            peer(&home, id, &path, mode == "writer").await?;
        }
        _ => anyhow::bail!("unsupported fixture mode"),
    }
    Ok(())
}

async fn create(home: &Path, recent: bool, isolated: bool, old_main: bool) -> anyhow::Result<()> {
    anyhow::ensure!(!home.exists(), "fixture destination must not exist");
    std::fs::create_dir_all(home.join("sessions"))?;
    std::fs::create_dir_all(home.join("archived_sessions"))?;
    std::fs::create_dir_all(home.join("thread-writer-locks"))?;
    std::fs::create_dir_all(home.join(".tmp"))?;
    let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.to_path_buf())?);
    let state = StateRuntime::init(sqlite.clone(), "openai".to_owned()).await?;
    let old = DateTime::parse_from_rfc3339(if recent {
        "2026-09-01T00:00:00Z"
    } else {
        "2026-06-01T00:00:00Z"
    })?
    .with_timezone(&Utc);
    let target = ThreadId::default();
    let main = ThreadId::default();
    let source = SessionSource::SubAgent(SubAgentSource::Other("fixture-worker".to_owned()));
    let target_path = write_history(home, target, old, source.clone())?;
    let metadata =
        ThreadMetadataBuilder::new(target, target_path.clone(), old, source).build("openai");
    state.upsert_thread(&metadata).await?;
    let main_time = if old_main { old } else { Utc::now() };
    let main_path = write_history(home, main, main_time, SessionSource::Cli)?;
    state
        .upsert_thread(
            &ThreadMetadataBuilder::new(main, main_path.clone(), main_time, SessionSource::Cli)
                .build("openai"),
        )
        .await?;
    if !isolated {
        state
            .thread_queue()
            .enqueue(target, "{\"old\":true}")
            .await?;
    }
    state
        .thread_queue()
        .enqueue(main, "{\"retained\":true}")
        .await?;
    for (id, time) in [(target, old), (main, main_time)] {
        if isolated && id == target {
            continue;
        }
        state
            .thread_goals()
            .replace_thread_goal_snapshot(&ThreadGoal {
                thread_id: id,
                goal_id: ThreadId::default().to_string(),
                objective: format!("retained goal {id}"),
                status: ThreadGoalStatus::Paused,
                token_budget: None,
                tokens_used: 0,
                time_used_seconds: 0,
                created_at: time,
                updated_at: time,
            })
            .await?;
        let Stage1JobClaimOutcome::Claimed { ownership_token } = state
            .memories()
            .try_claim_stage1_job(id, id, time.timestamp(), 60, 10)
            .await?
        else {
            anyhow::bail!("native stage1 claim unavailable")
        };
        anyhow::ensure!(
            state
                .memories()
                .mark_stage1_job_succeeded(
                    id,
                    &ownership_token,
                    time.timestamp(),
                    &format!("memory {id}"),
                    "isolated summary",
                    None
                )
                .await?,
            "native stage1 completion failed"
        );
    }
    state.close().await;
    let history_pool = codex_state::open_thread_history_db(&sqlite).await?;
    history_pool.close().await;
    let pool = sqlite.open_read_write_pool(&sqlite.logs_db_path()).await?;
    sqlx::query("INSERT INTO logs(ts, ts_nanos, level, target, feedback_log_body, thread_id) VALUES (?, 0, 'INFO', 'fixture', 'old diagnostic', ?)")
        .bind(old.timestamp()).bind(target.to_string()).execute(&pool).await?;
    sqlx::query("INSERT INTO logs(ts, ts_nanos, level, target, feedback_log_body, thread_id) VALUES (?, 0, 'INFO', 'fixture', 'retained diagnostic', ?)")
        .bind(main_time.timestamp()).bind(main.to_string()).execute(&pool).await?;
    pool.close().await;
    println!(
        "{}",
        serde_json::json!({"home":home,"target":target,"main":main,"target_path":target_path,"main_path":main_path})
    );
    Ok(())
}

async fn add_work(home: &Path, target: ThreadId, mode: &str) -> anyhow::Result<()> {
    let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.to_path_buf())?);
    let state = StateRuntime::init(sqlite.clone(), "openai".to_owned()).await?;
    let old = DateTime::parse_from_rfc3339("2026-06-01T00:00:00Z")?.with_timezone(&Utc);
    match mode {
        "add-pending" | "add-old-pending" => {
            // Native storage accepts opaque payloads; this does not qualify a
            // public TurnInput. The old case retains that pending owner while
            // varying its fixture-owned persistence timestamp.
            state
                .thread_queue()
                .enqueue(target, "{\"pending\":true}")
                .await?;
            if mode == "add-old-pending" {
                let pool = sqlite.open_read_write_pool(&sqlite.queue_db_path()).await?;
                sqlx::query(
                    "UPDATE queued_items SET created_at_ms=?, updated_at_ms=? WHERE thread_id=?",
                )
                .bind(old.timestamp_millis())
                .bind(old.timestamp_millis())
                .bind(target.to_string())
                .execute(&pool)
                .await?;
                pool.close().await;
            }
        }
        "add-goal" | "add-deferral" | "add-active-goal" => {
            state
                .thread_goals()
                .replace_thread_goal_snapshot(&ThreadGoal {
                    thread_id: target,
                    goal_id: ThreadId::default().to_string(),
                    objective: "unfinished retained work".to_owned(),
                    status: if mode == "add-active-goal" {
                        ThreadGoalStatus::Active
                    } else {
                        ThreadGoalStatus::Paused
                    },
                    token_budget: None,
                    tokens_used: 0,
                    time_used_seconds: 0,
                    created_at: old,
                    updated_at: old,
                })
                .await?;
        }
        "add-running-job" | "add-expired-job" | "add-done-job" | "add-output" => {
            let Stage1JobClaimOutcome::Claimed { ownership_token } = state
                .memories()
                .try_claim_stage1_job(
                    target,
                    target,
                    old.timestamp(),
                    if mode == "add-expired-job" { 0 } else { 60 },
                    10,
                )
                .await?
            else {
                anyhow::bail!("native stage1 claim unavailable")
            };
            if matches!(mode, "add-done-job" | "add-output") {
                anyhow::ensure!(
                    state
                        .memories()
                        .mark_stage1_job_succeeded(
                            target,
                            &ownership_token,
                            old.timestamp(),
                            "old memory",
                            "isolated summary",
                            None
                        )
                        .await?,
                    "native stage1 completion failed"
                );
            }
            if mode == "add-output" {
                let pool = sqlite
                    .open_read_write_pool(&sqlite.memories_db_path())
                    .await?;
                sqlx::query("DELETE FROM jobs WHERE kind='memory_stage1' AND job_key=?")
                    .bind(target.to_string())
                    .execute(&pool)
                    .await?;
                pool.close().await;
            }
        }
        _ => anyhow::bail!("unsupported work fixture mode"),
    }
    state.close().await;
    Ok(())
}

fn message(text: String) -> RolloutItem {
    RolloutItem::ResponseItem(
        ResponseItem::Message {
            id: None,
            role: "user".to_owned(),
            content: vec![ContentItem::InputText { text }],
            phase: None,
            internal_chat_message_metadata_passthrough: None,
        }
        .into(),
    )
}

fn write_history(
    home: &Path,
    id: ThreadId,
    time: DateTime<Utc>,
    source: SessionSource,
) -> anyhow::Result<PathBuf> {
    let path = home.join("sessions").join(format!(
        "rollout-{}-{id}.jsonl",
        time.format("%Y-%m-%dT%H-%M-%S")
    ));
    let meta = SessionMeta {
        id,
        session_id: SessionId::from(id),
        timestamp: time.to_rfc3339(),
        cwd: home.to_path_buf(),
        source,
        model_provider: Some("openai".to_owned()),
        history_mode: ThreadHistoryMode::Legacy,
        originator: "native-fixture".to_owned(),
        cli_version: "0.162.1".to_owned(),
        ..Default::default()
    };
    let lines = [
        RolloutLine {
            timestamp: time.to_rfc3339(),
            ordinal: Some(0),
            item: RolloutItem::SessionMeta(SessionMetaLine { meta, git: None }),
        },
        RolloutLine {
            timestamp: time.to_rfc3339(),
            ordinal: Some(1),
            item: message("history content ".repeat(8192)),
        },
    ];
    let mut file = std::fs::File::create(&path)?;
    for line in lines {
        serde_json::to_writer(&mut file, &line)?;
        writeln!(file)?;
    }
    file.sync_all()?;
    Ok(path)
}

async fn peer(home: &Path, id: ThreadId, path: &Path, writer: bool) -> anyhow::Result<()> {
    let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.to_path_buf())?);
    let state = StateRuntime::init(sqlite.clone(), "openai".to_owned()).await?;
    let store = LocalThreadStore::new(
        LocalThreadStoreConfig {
            codex_home: home.to_path_buf(),
            sqlite: sqlite.clone(),
            default_model_provider_id: "openai".to_owned(),
        },
        Some(state.clone()),
    );
    let recorder = if writer {
        let coordinator = Arc::new(WriterLockCoordinator::new(home));
        let guard = Arc::new(coordinator.acquire(id)?);
        let config = RolloutConfig {
            codex_home: home.to_path_buf(),
            sqlite,
            cwd: home.to_path_buf(),
            model_provider_id: "openai".to_owned(),
            generate_memories: false,
        };
        Some(
            RolloutRecorder::new_with_writer_lock(
                &config,
                RolloutRecorderParams::Resume {
                    path: path.to_path_buf(),
                },
                guard,
            )
            .await?,
        )
    } else {
        None
    };
    println!("ready");
    io::stdout().flush()?;
    for command in io::stdin().lock().lines() {
        match command?.as_str() {
            "append" => {
                recorder
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("no recorder"))?
                    .record_canonical_items(&[message("MAIN continues".to_owned())])
                    .await?;
                recorder
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("fixture recorder is absent"))?
                    .flush()
                    .await?;
                println!("appended");
            }
            "queue" => {
                state
                    .thread_queue()
                    .enqueue(id, "{\"additional\":true}")
                    .await?;
                println!("queued");
            }
            "state-name" => {
                let started = std::time::Instant::now();
                let result = state
                    .update_thread_name(id, Some("MAIN state preserved"))
                    .await;
                println!(
                    "{}",
                    serde_json::json!({"state_changed":matches!(&result, Ok(true)),"state_error":result.as_ref().err().map(ToString::to_string),"elapsed_ms":started.elapsed().as_millis()})
                );
            }
            "index-name" => {
                codex_rollout::append_thread_name(home, id, "MAIN name preserved").await?;
                println!("named");
            }
            "archive" => {
                store
                    .archive_thread(ArchiveThreadParams { thread_id: id })
                    .await?;
                println!("archived");
            }
            "name" => {
                let result = store
                    .update_thread_metadata(UpdateThreadMetadataParams {
                        thread_id: id,
                        patch: ThreadMetadataPatch {
                            name: Some(Some("pending target name".to_owned())),
                            ..Default::default()
                        },
                        include_archived: false,
                    })
                    .await;
                println!(
                    "{}",
                    serde_json::json!({"name_ok":result.is_ok(),"name_error":result.err().map(|error| error.to_string())})
                );
            }
            "exit" => break,
            _ => anyhow::bail!("unsupported peer command"),
        }
        io::stdout().flush()?;
    }
    if let Some(recorder) = recorder {
        recorder.shutdown().await?;
    }
    state.close().await;
    Ok(())
}

struct FixtureBoardHost(ThreadId);
type BoardFuture<'a, T> = std::pin::Pin<
    Box<dyn std::future::Future<Output = codex_protocol::error::Result<T>> + Send + 'a>,
>;
impl codex_agent_message_board_extension::MessageBoardHost for FixtureBoardHost {
    fn agent_path(&self, _caller: ThreadId) -> BoardFuture<'_, codex_protocol::AgentPath> {
        Box::pin(async { Ok(codex_protocol::AgentPath::root()) })
    }
    fn resolve_agent(&self, _path: codex_protocol::AgentPath) -> BoardFuture<'_, ThreadId> {
        Box::pin(async { Ok(self.0) })
    }
    fn current_time(&self, _caller: ThreadId) -> BoardFuture<'_, DateTime<Utc>> {
        Box::pin(async { Ok(Utc::now()) })
    }
    fn notify(
        &self,
        _recipient: ThreadId,
        _post: codex_agent_message_board_extension::PostPreview,
    ) -> BoardFuture<'_, codex_agent_message_board_extension::NotificationDelivery> {
        Box::pin(async {
            Ok(codex_agent_message_board_extension::NotificationDelivery::SkippedInactive)
        })
    }
}

async fn board_peer(home: &Path, id: ThreadId) -> anyhow::Result<()> {
    use codex_agent_message_board_extension::{
        CreateChannelRequest, LocalAgentMessageBoard, SubscriptionChange,
    };
    let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.to_path_buf())?);
    let board =
        LocalAgentMessageBoard::open(&sqlite, id.into(), Arc::new(FixtureBoardHost(id))).await?;
    println!("ready");
    io::stdout().flush()?;
    for line in io::stdin().lock().lines() {
        let channel_name = line?;
        if channel_name == "exit" {
            break;
        }
        let result = board
            .create_channel(
                id,
                CreateChannelRequest {
                    channel_name,
                    subscription: SubscriptionChange::Unsubscribe,
                },
            )
            .await;
        println!(
            "{}",
            serde_json::json!({"write_ok":result.is_ok(),"error":result.err().map(|error|error.to_string())})
        );
        io::stdout().flush()?;
    }
    Ok(())
}

fn publication_params(
    home: &Path,
    thread_id: ThreadId,
    source: Option<ThreadId>,
    history_base: Option<codex_protocol::protocol::HistoryPosition>,
) -> codex_thread_store::CreateThreadParams {
    codex_thread_store::CreateThreadParams {
        creator_user_id: None,
        creator_account_id: None,
        session_id: thread_id.into(),
        thread_id,
        extra_config: None,
        forked_from_id: source,
        parent_thread_id: None,
        source: SessionSource::Exec,
        thread_source: None,
        originator: "publication-fixture".to_owned(),
        base_instructions: codex_protocol::models::BaseInstructions::default(),
        dynamic_tools: Vec::new(),
        selected_capability_roots: Vec::new(),
        multi_agent_version: None,
        history_mode: ThreadHistoryMode::Paginated,
        history_base,
        subagent_history_start_ordinal: None,
        initial_window_id: "publication-window".to_owned(),
        runtime_workspace_roots: None,
        metadata: ThreadPersistenceMetadata {
            cwd: Some(home.to_path_buf()),
            model_provider: "openai".to_owned(),
            memory_mode: codex_protocol::protocol::ThreadMemoryMode::Disabled,
        },
    }
}

async fn publication_fixture(
    home: &Path,
    mode: &str,
    identity: Option<&String>,
) -> anyhow::Result<()> {
    std::fs::create_dir_all(home)?;
    let sqlite = SqliteConfig::from_sqlite_home(AbsolutePathBuf::try_from(home.to_path_buf())?);
    let state = StateRuntime::init(sqlite.clone(), "openai".to_owned()).await?;
    let store = LocalThreadStore::new(
        LocalThreadStoreConfig {
            codex_home: home.to_path_buf(),
            sqlite,
            default_model_provider_id: "openai".to_owned(),
        },
        Some(state.clone()),
    );
    let thread_id = match identity {
        Some(value) => ThreadId::from_string(value)?,
        None => ThreadId::default(),
    };
    match mode {
        "create-paginated" | "create-reverted" => {
            store
                .create_thread(publication_params(home, thread_id, None, None))
                .await?;
            store
                .append_items(codex_thread_store::AppendThreadItemsParams {
                    thread_id,
                    items: vec![message("publication-source-marker".to_owned())],
                })
                .await?;
            if mode == "create-reverted" {
                use codex_protocol::protocol::{EventMsg, TurnCompleteEvent, TurnStartedEvent};
                let mut items = Vec::new();
                for turn_id in ["fixture-turn-1", "fixture-turn-2"] {
                    items.push(RolloutItem::EventMsg(EventMsg::TurnStarted(
                        TurnStartedEvent {
                            turn_attribution: None,
                            turn_id: turn_id.to_owned(),
                            root_turn_id: None,
                            trace_id: None,
                            started_at: Some(10),
                            model_context_window: None,
                            collaboration_mode_kind: Default::default(),
                        },
                    )));
                    items.push(RolloutItem::EventMsg(EventMsg::TurnComplete(
                        TurnCompleteEvent {
                            root_turn_id: None,
                            turn_id: turn_id.to_owned(),
                            last_agent_message: None,
                            error: None,
                            started_at: Some(10),
                            completed_at: Some(20),
                            duration_ms: Some(10_000),
                            time_to_first_token_ms: None,
                        },
                    )));
                }
                store
                    .append_items(codex_thread_store::AppendThreadItemsParams { thread_id, items })
                    .await?;
            }
            store
                .persist_thread(thread_id, codex_thread_store::PersistContext::Standard)
                .await?;
            publish_fixture_metadata(&store, thread_id).await?;
            store.shutdown_thread(thread_id).await?;
            if mode == "create-reverted" {
                store
                    .revert_thread(codex_thread_store::RevertThreadParams {
                        thread_id,
                        before_turn_id: "fixture-turn-2".to_owned(),
                        multi_agent_version: None,
                    })
                    .await?;
            }
            let metadata = state
                .get_thread(thread_id)
                .await?
                .ok_or_else(|| anyhow::anyhow!("source metadata absent after persistence"))?;
            println!(
                "{}",
                serde_json::json!({"id":thread_id,"rollout_id":codex_rollout::rollout_id_from_path(metadata.rollout_path.as_path()),"path":metadata.rollout_path})
            );
        }
        "fork-peer" => {
            let prepared = store
                .prepare_fork(codex_thread_store::PrepareForkParams {
                    thread_id,
                    boundary: codex_thread_store::ForkBoundary::Latest,
                })
                .await?;
            let child_id = ThreadId::default();
            println!(
                "{}",
                serde_json::json!({"stage":"prepared","source_thread_id":prepared.source_thread_id,
                "history_base":prepared.history_base,"model_context_items":prepared.model_context.len(),"child_thread_id":child_id})
            );
            io::stdout().flush()?;
            let mut command = String::new();
            io::stdin().read_line(&mut command)?;
            if command.trim() == "publish" {
                let publication: anyhow::Result<PathBuf> = async {
                    store
                        .create_thread(publication_params(
                            home,
                            child_id,
                            Some(thread_id),
                            prepared.history_base,
                        ))
                        .await?;
                    store
                        .persist_thread(child_id, codex_thread_store::PersistContext::Standard)
                        .await?;
                    publish_fixture_metadata(&store, child_id).await?;
                    store.shutdown_thread(child_id).await?;
                    let metadata = state.get_thread(child_id).await?.ok_or_else(|| {
                        anyhow::anyhow!("child metadata absent after persistence")
                    })?;
                    Ok(metadata.rollout_path)
                }
                .await;
                match publication {
                    Ok(path) => println!(
                        "{}",
                        serde_json::json!({"stage":"published","child_thread_id":child_id,"path":path})
                    ),
                    Err(error) => println!(
                        "{}",
                        serde_json::json!({"stage":"publication_failed","child_thread_id":child_id,"error":error.to_string()})
                    ),
                }
            } else {
                println!("{}", serde_json::json!({"stage":"aborted"}));
            }
            drop(prepared);
        }
        "delete-native" => {
            match store
                .delete_thread(codex_thread_store::DeleteThreadParams { thread_id })
                .await
            {
                Ok(()) => println!(
                    "{}",
                    serde_json::json!({"deleted":true,"thread_id":thread_id})
                ),
                Err(error) => println!(
                    "{}",
                    serde_json::json!({"deleted":false,"thread_id":thread_id,"error":error.to_string()})
                ),
            }
        }
        "read-lineage" => {
            match store
                .load_latest_model_context(codex_thread_store::LoadThreadHistoryParams {
                    thread_id,
                    include_archived: true,
                })
                .await
            {
                Ok(history) => {
                    let marker_present = history.items.iter().any(|item| {
                        let RolloutItem::ResponseItem(response) = item else { return false; };
                        let ResponseItem::Message {content,..} = &response.item else { return false; };
                        content.iter().any(|part| matches!(part, ContentItem::InputText {text} if text == "publication-source-marker"))
                    });
                    println!(
                        "{}",
                        serde_json::json!({"readable":true,"item_count":history.items.len(),"source_marker_present":marker_present})
                    );
                }
                Err(error) => println!(
                    "{}",
                    serde_json::json!({"readable":false,"error":error.to_string()})
                ),
            }
        }
        _ => anyhow::bail!("unsupported publication fixture mode"),
    }
    state.close().await;
    Ok(())
}

async fn publish_fixture_metadata(
    store: &LocalThreadStore,
    thread_id: ThreadId,
) -> anyhow::Result<()> {
    store
        .update_thread_metadata(UpdateThreadMetadataParams {
            thread_id,
            include_archived: false,
            patch: ThreadMetadataPatch {
                name: Some(Some("publication-fixture".to_owned())),
                source: Some(SessionSource::Exec),
                model_provider: Some("openai".to_owned()),
                ..Default::default()
            },
        })
        .await?;
    Ok(())
}

/// Runs the stock producer in a disposable one-file home. Process exit remains the
/// final runtime-drain fence: the existing Tokio main waits for blocking jobs on
/// shutdown. A timeout report never claims that the worker or publication finished.
async fn stock_compression_fixture(home: &Path, source: &Path) -> anyhow::Result<()> {
    use std::time::{Duration, Instant, SystemTime};

    let started = Instant::now();
    let compressed = source.with_extension("jsonl.zst");
    let marker = home.join(".tmp/rollout-compression.lock");
    let mut marker_seen = false;
    let mut source_bytes = None;
    let outcome: anyhow::Result<serde_json::Value> = async {
        anyhow::ensure!(source.is_absolute(), "source rollout path must be absolute");
        let canonical_home = std::fs::canonicalize(home)?;
        let canonical_source = std::fs::canonicalize(source)?;
        anyhow::ensure!(
            canonical_source.starts_with(canonical_home.join("sessions"))
                || canonical_source.starts_with(canonical_home.join("archived_sessions")),
            "source must be inside the disposable home's rollout directories"
        );
        let metadata = std::fs::symlink_metadata(source)?;
        anyhow::ensure!(metadata.file_type().is_file(), "source must be a regular plain file");
        anyhow::ensure!(source.extension().is_some_and(|extension| extension == "jsonl"), "source must have the plain JSONL extension");
        anyhow::ensure!(codex_rollout::rollout_id_from_path(source).is_some(), "source requires a valid rollout filename");
        source_bytes = Some(metadata.len());
        anyhow::ensure!(metadata.len() <= 16 * 1024 * 1024, "source exceeds this fixture's admitted 16 MiB input budget");
        anyhow::ensure!(SystemTime::now().duration_since(metadata.modified()?)? >= Duration::from_secs(7 * 24 * 60 * 60), "source is not cold under the stock seven-day rule");
        anyhow::ensure!(!compressed.try_exists()?, "fresh fixture must not contain a compressed sibling");
        anyhow::ensure!(!marker.try_exists()?, "fresh fixture must not contain a prior compression run marker");
        // The stock worker itself parses the actual SessionMeta and performs
        // level-three encoding, pledged source size, verification and publication.
        codex_rollout::spawn_rollout_compression_worker(
            home.to_path_buf(),
            codex_rollout::RolloutCompressionTrigger::Rpc,
        );
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            marker_seen |= marker.try_exists()?;
            if marker_seen
                && let Some(guard) = codex_rollout::try_acquire_rollout_maintenance_lock(home)?
            {
                let source_retained = source.try_exists()?;
                let compressed_metadata = match std::fs::symlink_metadata(&compressed) {
                    Ok(metadata) => Some(metadata),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => None,
                    Err(error) => return Err(error.into()),
                };
                let compressed_present = compressed_metadata.as_ref().is_some_and(|value| value.file_type().is_file());
                let compressed_bytes = compressed_metadata.as_ref().map(std::fs::Metadata::len);
                drop(guard);
                return Ok(serde_json::json!({
                    "status":if !source_retained && compressed_present {"published"} else {"completed_without_publication"},
                    "input_path":source,"compressed_path":compressed,"source_bytes":source_bytes,
                    "compressed_bytes":compressed_bytes,"source_retained":source_retained,
                    "compressed_present":compressed_present,"run_marker_observed":marker_seen,
                    "maintenance_guard_acquired":true,"maintenance_guard_released":true,
                    "worker_pass_finished":true,"elapsed_ms":started.elapsed().as_millis(),
                    "process_exit_is_runtime_drain_fence":true
                }));
            }
            if Instant::now() >= deadline {
                return Ok(serde_json::json!({
                    "status":"timeout","input_path":source,"compressed_path":compressed,
                    "source_bytes":source_bytes,"compressed_bytes":null,
                    "source_retained":source.try_exists()?,"compressed_present":compressed.try_exists()?,
                    "run_marker_observed":marker_seen,"maintenance_guard_acquired":false,
                    "maintenance_guard_released":false,"worker_pass_finished":false,
                    "elapsed_ms":started.elapsed().as_millis(),"process_exit_is_runtime_drain_fence":true
                }));
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }.await;
    match outcome {
        Ok(report) => {
            println!("{report}");
            if report["status"].as_str() == Some("published") {
                Ok(())
            } else {
                anyhow::bail!(
                    "stock compression did not establish publication: {}",
                    report["status"]
                )
            }
        }
        Err(error) => {
            println!(
                "{}",
                serde_json::json!({"status":"error","input_path":source,
                "compressed_path":compressed,"source_bytes":source_bytes,"compressed_bytes":null,
                "run_marker_observed":marker_seen,"worker_pass_finished":false,
                "maintenance_guard_acquired":false,"maintenance_guard_released":false,
                "elapsed_ms":started.elapsed().as_millis(),"error":error.to_string(),
                "process_exit_is_runtime_drain_fence":true})
            );
            Err(error)
        }
    }
}
