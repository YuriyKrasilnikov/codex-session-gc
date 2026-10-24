# Synthetic storage fixtures

`native-state-profile.json` supplies the `threads` and `thread_spawn_edges` DDL
and one artificial row template. Catalogue tests and synthetic catalogue
measurements replace its IDs, paths, source and measured fields when constructing
disposable homes. Its template path is synthetic and never opened as a source.

`../plan_storage.json` supplies five artificial paginated histories and SQLite
DDL/keyed rows for read-only selection tests. A root, child and grandchild form
a spawn chain; a separate fork refers to the root's exact synthetic history
prefix; the fifth history is archived. Nonempty user/assistant records, SQL turn
and item keys, timestamps, and prefix byte/ordinal boundaries are explicit.
The loader substitutes only `{home}` into paths when materializing a test home.
Migration installation timestamps and backfill clocks use the fixed synthetic
instant 2026-01-01 UTC; migration execution durations are zero.

These inputs test catalogue, selection and preparation behavior. They neither
qualify an installed Codex runtime nor establish native writer exclusion.
