pub use codex_thread_store::LocalThreadStore;

#[cfg(test)]
mod tests {
    use codex_protocol::ContextWindowId;
    use codex_protocol::protocol::SessionContextWindow;
    use serde_json::json;

    #[test]
    fn persisted_context_window_retains_text_and_generated_allocation() {
        let text = json!({"window_id": "0190199d-f000-7000-8000-000000000000"});
        let generated = json!({"window_id": {"salt": "window", "ordinal": 3, "value": 42}});
        for value in [text, generated] {
            let window: SessionContextWindow =
                serde_json::from_value(value.clone()).expect("valid persisted window decodes");
            assert_eq!(
                serde_json::to_value(&window).expect("window reserializes"),
                value
            );
            assert!(window.window_id.validated().is_some());
        }
        let generated: SessionContextWindow = serde_json::from_value(
            json!({"window_id": {"salt": "item", "ordinal": 3, "value": 42}}),
        )
        .expect("generated item ID decodes before validation");
        assert!(matches!(generated.window_id, ContextWindowId::Generated(_)));
        assert!(generated.window_id.validated().is_none());
        assert!(
            serde_json::from_value::<SessionContextWindow>(
                json!({"window_id": {"salt": "window"}})
            )
            .is_err()
        );
    }
}
