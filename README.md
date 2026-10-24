# codex-session-gc

Browse Codex storage, select closed work and delete an exact set of histories
while unrelated sessions continue running. The terminal interface uses Textual;
CLI and MCP operations share its selection rules. MAIN histories can be selected.
Explicit Guardian histories can also be selected. Their historical parent association
does not select or require deletion of the parent. Age is a caller-supplied filter,
not a fixed retention policy.

## Download and install

Get the matched Linux bundle from
[release v0.2.0](https://github.com/YuriyKrasilnikov/codex-session-gc/releases/tag/v0.2.0).
It contains the Python wheel, qualified native helper, hash-locked requirements,
license notices and `manifest.json`. The generic Python wheel alone does not
contain the helper needed for deletion.

| Component | Release 0.2.0 boundary |
| --- | --- |
| Qualified host | Linux x86_64, Ubuntu 26.04 LTS, glibc 2.43 |
| Python | Requires 3.14 or newer; tested with 3.14.7 |
| Native contract | Wire 6, state migrations through 61 and exact companion schemas, including the qualified board template schema |
| Compatibility authority | The wheel's bundled contract and release manifest; a Codex version string alone is insufficient |

Other operating systems, architectures and libc environments require cleanup
qualification. Native tests cover recorder/storage ownership and bounded effects;
see [supported behavior](docs/contract.md) and
[native qualification](native/README.md) for the lifecycle limits.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and an existing
Python 3.14 interpreter first. These commands use `python3.14` from PATH and do
not download Python. Run them in a new download directory:

```sh
set -eu
mkdir codex-session-gc-download
cd codex-session-gc-download
curl --fail --location --remote-name https://github.com/YuriyKrasilnikov/codex-session-gc/releases/download/v0.2.0/codex-session-gc-0.2.0-linux-x86_64.tar.gz
curl --fail --location --remote-name https://github.com/YuriyKrasilnikov/codex-session-gc/releases/download/v0.2.0/SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf codex-session-gc-0.2.0-linux-x86_64.tar.gz
cd codex-session-gc-0.2.0-linux-x86_64
gc_install_dir="$HOME/.local/share/codex-session-gc/0.2.0/venv"
test ! -e "$gc_install_dir"
uv venv --no-project --no-python-downloads --python python3.14 "$gc_install_dir"
uv pip install --python "$gc_install_dir/bin/python" --require-hashes --only-binary :all: -r install-requirements.txt
. "$gc_install_dir/bin/activate"
codex-session-gc ui --helper "$PWD/codex-session-gc-native"
```

The checksum command verifies the downloaded release assets against `SHA256SUMS`;
missing assets for other download choices are ignored. If the installation path
already exists, choose a new empty path and use it in the MCP registration too;
do not clear an existing environment. Keep the unpacked bundle
at its chosen absolute location: CLI/MCP retain the helper path you supply.
Installation must run from the bundle directory so its local wheel reference
resolves. Runtime dependencies may need downloading; no Rust toolchain is needed.

The browser uses `CODEX_HOME` or `~/.codex`; `--home` selects another directory.
Read the preview before confirming deletion. For interruption or an upgrade from
an existing installation, follow [recovery and upgrades](docs/recovery.md).

## Source checkout

```sh
git clone https://github.com/YuriyKrasilnikov/codex-session-gc.git
cd codex-session-gc
uv sync --locked
```

Run `.venv/bin/python -m codex_session_gc` in place of `codex-session-gc` below.
For cleanup, download and retain the qualified helper from the matching release
bundle, then pass its absolute path with `--helper`. A source rebuild with a
different binary digest requires separate qualification and a new matched release;
it is not an installation shortcut. See [native reconstruction and package
assembly](native/README.md), [architecture](docs/architecture.md) and
[Codex updates](docs/compatibility.md).

## License

The project is licensed under [Apache-2.0](LICENSE). [NOTICE](NOTICE) retains
upstream attribution. The `third_party` directory contains dependency notices and
their source index; these accompany source packages, wheels and release bundles.

## Browse and select

The browser opens with the largest work groups first. Each group shows its own
stored bytes, agent bytes and total across distinct physical histories, including
plain and Zstandard representations and pre/post-revert rollouts. Known observed
bytes are kept separately from an exact total: an incomplete observation shows
`≥known` and an unknown total. Sorting ranks the known observed amount; it does
not claim to order unknown actual totals. Equal known amounts put exact totals
before incomplete totals, then use the stable session ID. These are stored lengths, not promised
recovered disk space. Shared providers and conditional reclaim remain unknown
until the complete cleanup preview. MAIN, ordinary agent, Guardian and
unknown source labels come from stored source metadata; a missing parent does
not make a history MAIN.

Filter by text, last activity, stored-byte bounds, history type and work group.
A minimum byte bound can be proved by the known lower bound; a maximum requires
an exact total. Both bounds apply together. Sort by total size, own size, agent
size or activity. The date matches each
history whose known last activity is strictly earlier than the selected UTC
instant. Group activity is shown separately. The initial date is visible and
editable; it does not select anything automatically.

Enter expands a group. Space toggles the focused matching history; Select matches
selects matching histories without selecting a contextual parent automatically.
Changing filters preserves selected IDs. Refresh cannot add deletion IDs.

Preview reads scoped SQL metadata and bounded metadata headers across the managed
physical namespace, including orphan consumers. It does
not copy databases or read complete history bodies. Review the exact IDs, cutoff,
sizes and retention reasons. Type literal `DELETE` to dispatch once. Native
ownership, history-provider and current-state checks may still refuse or narrow
the conditional selection. A catalogue lifecycle label is not proof of closure.

Esc or Cancel stops and reaps the preview worker before dispatch. After dispatch,
the interface joins the owned execution and preserves its result and receipt.
For partial or unknown results, keep the receipt and reconcile; do not repeat the
original delete command merely because its reply was lost.

`ui --cutoff` sets the visible initial UTC boundary. `--receipt-dir` chooses the
durable receipt directory, which must be outside the selected Codex home. Its
default is the user's state directory. The UI requires an interactive terminal.

## CLI preview and cleanup

Choose a closed session root and an explicit UTC cutoff:

```console
codex-session-gc plan --online --home /absolute/codex-home \
  --cutoff 2026-08-01T00:00:00Z --closed-work PARENT_UUID --json
```

The date above is an example. Repeat `--closed-work` for independent roots.
By default the requested set includes each root and its spawned descendants.
Repeat `--select-thread UUID` to request only particular histories instead:

```console
codex-session-gc apply --online --home /absolute/codex-home \
  --cutoff 2026-08-01T00:00:00Z --closed-work PARENT_UUID \
  --select-thread CHILD_UUID --helper /absolute/codex-session-gc-native \
  --receipt /absolute/new-cleanup.sqlite --json
```

Omitting `--select-thread` considers the selected work's whole membership against
the cutoff and retention rules. The cutoff narrows deletion eligibility, not the
ownership scope. Retained descendants prevent selecting their cascading parent.
Unknown origin, age, archive state, missing history and ambiguous topology remain
visible as retention or refusal. Known MAIN and archive states are not automatic
policy exclusions; the native contract determines supported effects.

Preview JSON contains `roots`, `selected`, conditional `deletions`, corresponding
`sizes` in stored bytes, `retained` reasons and `errors`. `physical_manifest` binds
every selected owner's actual representation, immutable rollout ID, path, stat,
header digest and independent history/fork metadata. `shared_retained_bytes`
counts distinct transitive providers outside the selected physical set;
`conditional_reclaim_bytes` is the selected stored total, not guaranteed disk
reclaim. `incomingReferencesComplete` records complete observed provider headers;
`closureCertified` remains false. These observations are not an atomic snapshot
or a certificate that writers have released ownership. Preview is read-only and non-executable. Apply takes fresh observations;
it does not execute a saved preview file. Guardian previews identify their historical
parent association. UI confirmation and MCP declarations bind that association; a
changed association is refused before dispatch. Direct CLI apply derives fresh facts.

Apply reports every requested root, selected subset, retained reason and native
outcome, including work never dispatched. Confirmed eligible work alongside a
skipped root produces a partial aggregate. When no selected history is eligible,
no helper or receipt is created. `--default-provider` defaults to `openai`;
individual stored providers are checked separately.

## Results after interruption

Use only the saved receipt:

```console
codex-session-gc reconcile --online --receipt /absolute/cleanup.sqlite --json
```

Reconcile accepts no home, helper or selection override. It never sends another
deletion request or continues work that was not dispatched. Its scope is
`saved_journal_units`; roots skipped before receipt creation are outside that
scope. Original delivery outcomes and observed final states remain distinct.

Completed current-format journal records can be displayed using their saved
metadata even after the qualified helper changes. A fresh observation of an
unresolved request requires the exact current profile and wire format. Retired
profiles and journal schemas are not converted or tried automatically.
`CONFIRMED_FINAL_STATE` means the selected effects were observed absent within
the declared bound; it does not attribute every effect to a particular invocation
or measure reclaimed space.

Plan exits 0 for an available conditional selection and 3 for unavailable facts
or an invalid selection. Apply and reconcile exit 0 for confirmed, 3 for refused,
and 4 for partial or unknown. Invalid arguments exit 2.

## Storage and resource contract

Catalogue reads metadata in a short read-only live-WAL transaction, closes it,
then walks active/archive names and obtains stat identities through safe directory
descriptors. Each catalogue observation owns one pool of at most eight workers.
Workers obtain full nofollow metadata through borrowed parent descriptors, in
chunks of at most 16 sibling names. At most eight chunks are outstanding; results
are reduced in enumeration order. Each batch settles before recursion or parent
descriptor closure, and the pool is joined before observation returns. Catalogue
teardown waits for this work. A blocked filesystem call has no finite completion
guarantee. Catalogue never opens history content. Filename ownership and immutable
rollout identity distinguish reverted histories and competing copies; ambiguous
objects are attributed to no owner's known subtotal. Missing namespace coverage
retains known lower bounds without inventing an exact total. SQL history mode is
reported as SQL metadata; physical lineage remains explicitly unknown. Filtering,
grouping and paging use detached metadata. Independent databases do not form one
global concurrent snapshot. Path checks reject observed invalid paths and changed
endpoint identities; they do not establish uninterrupted namespace ownership.

Preparation reads scoped metadata, selected effect keys and bounded first-line
physical headers. Native execution holds the work's writer ownership, walks bounded
namespace headers to protect retained providers, and revalidates current schema,
selected identities and exact SQL effects before deletion. Selected descriptors
remain open for the checks immediately before unlink. History bodies are not
scanned or hashed.

The supported native contract is wire 6 and the exact bundled state schema
through migration 61, with the qualified companion database schemas. The same
qualified path supports plain JSONL and Zstandard, legacy and paginated history,
and every observed pre/post-revert rollout belonging to a selected owner.
Historical physical metadata may differ from the current SQL provider or role;
the current header must agree with current SQL. Unsupported schema identities,
old wire versions and ambiguous competing representations refuse.
A selected Guardian requires matching SQL/header source identity, a valid historical
parent association and no incoming spawn edge. Its associated parent need not exist
or be closed; selecting that parent never implicitly selects the Guardian. Exact
selected-owner rows in `guardian_review_feedback` and `thread_read_receipts` are
deleted for both ordinary and Guardian histories; unselected opaque rows remain
untouched. Matched retained Guardians keep their existing physical writer guards.
Native recorder/storage and retained-history resume witnesses do not establish a
live Core, GuardianPool or app-server lifecycle guarantee.

`--max-read-bytes` defaults to 256 GiB and `--timeout-seconds` to six hours per
collection or dispatch. These are cooperative limits, not memory reservations or
kernel completion guarantees. Database and selected rollout extents are admission bounds;
unrelated and retained body lengths do not become global rollout-size limits.
SQLite page reads use a cooperative deadline rather than exact I/O accounting.
Requests are limited to 16 MiB, first-line metadata to 1 MiB including its newline,
and combined native output to 1 MiB. Bounds do not expand automatically.

The separately built helper uses Rust 1.99.1 and Edition 2024. See
[native build instructions](native/README.md). A new binary requires qualification
and a matching bundled identity; an arbitrary successful build is not accepted.

## MCP

The stdio server exposes `query_catalogue`, `session_details`, `prepare_cleanup`,
`apply_cleanup`, `cleanup_status` and `reconcile_cleanup`. Register it with absolute paths:

```console
codex mcp add codex-sessions -- "$HOME/.local/share/codex-session-gc/0.2.0/venv/bin/python" -m codex_session_gc mcp --home "$HOME/.codex" --helper "$PWD/codex-session-gc-native" --receipt-dir "$HOME/.local/state/codex-session-gc/receipts"
```

Run registration from the unpacked bundle directory. Replace `--home` when Codex
uses a different storage directory. Registration edits the client configuration;
starting the server itself does not.
Reconnect the client to load changed server code. Ask for the largest project
groups, agents last active before a chosen date, or a preview for explicit IDs.
Query and details share a cached catalogue identity; explicit refresh replaces it.
Preparation reads fresh scoped facts and returns a conditional report without a
journal or deletion. Pages contain at most 100 histories; preparation accepts at
most 200 distinct IDs per request. Larger selections can use the terminal UI.

Review the complete preparation report, then explicitly call `apply_cleanup` with
its unchanged `declaration`. Preparation supplies the operation UUID before apply,
exact roots and candidate IDs, cutoff, startup home identity, Guardian associations,
the exact physical manifest and qualified profile. Catalogue JSON uses
`stored_bytes` for an exact total or null, `known_stored_bytes` for the observed
lower bound, `physical_rollouts` for attributed objects and `physical_complete`
for complete membership. Work groups expose corresponding own/agent/total
known and exact stored-byte values plus incomplete-history counts. Initial
`physical_lineage` is `unknown`; shared-retained and conditional-reclaim values
are null until preparation.
The digest binds those inputs; it does not establish human consent. The helper
and receipt directory are fixed at server startup and cannot be chosen by tools.
The receipt directory must be outside the selected home. Without `--helper`, the
server discovers the qualified helper using the same rules as Textual.

Keep the operation UUID after cancellation, disconnect or a lost response. Read
`cleanup_status` or call `reconcile_cleanup` with that UUID. Reconciliation may
write observation evidence to the existing receipt and never dispatches deletion.
A duplicate apply observes the saved attempt; a changed declaration with the same
UUID is refused. Missing or unreadable receipts mean unknown, including when an
older call may still be preparing. Do not create a replacement operation to retry
an unknown deletion. Transport cancellation waits for owned backend cleanup;
receipts retain explicit unresolved lifecycle state when cleanup is unproven.
