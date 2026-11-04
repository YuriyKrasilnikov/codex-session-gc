# codex-session-gc

Browse Codex storage, select stopped work and delete an exact set of histories
while unrelated sessions continue running. The terminal interface uses Textual;
CLI and MCP operations share its selection rules. MAIN histories can be selected.
Explicit Guardian histories can also be selected. Their historical parent association
does not select or require deletion of the parent. Age is a caller-supplied filter,
not a fixed retention policy.

## What is new in 0.5.0

A matched installer stages releases alongside older consumers and reports an
explicit MCP command. The cleanup contract recognizes attached histories, the state
schema at migration 62, and stopped work whose spawn relationship still records
`Open`. Doctor and MCP can compare an explicitly
imported source-support record with the current helper and profile; that record
does not authorize cleanup. The terminal keeps essential controls visible while
its content scrolls in compact windows.

## Download and install

The 0.5.0 installation workflow uses a matched Linux x86_64 bundle and a
standard-library installer. Obtain the published assets and independently
verify their attestations before using them. Python 3.14 or newer, `uv` and a
separately trusted GitHub CLI (`gh`) are prerequisites. The qualified deployment
is Ubuntu 26.04 LTS, glibc 2.43;
other environments require qualification. The native contract uses wire 6, the
exact state schema and migration 62, and the specified companion schemas.

Obtain the full 40-character assembly source commit independently from a trusted
release record. Use a trusted `gh` whose `attestation verify --help` includes
`--repo`, `--signer-workflow`, `--source-digest`, `--source-ref`,
`--predicate-type`, `--deny-self-hosted-runners` and `--format`. Verify the
bootstrap before executing it; an unverified installer cannot verify itself:

```sh
set -eu
mkdir codex-session-gc-download
cd codex-session-gc-download
curl --fail --location --output codex-session-gc-installer-0.5.0.pyz \
  https://github.com/YuriyKrasilnikov/codex-session-gc/releases/download/v0.5.0/codex-session-gc-installer-0.5.0.pyz
GC_ASSEMBLY_COMMIT=FULL_40_CHARACTER_ASSEMBLY_COMMIT
gh attestation verify codex-session-gc-installer-0.5.0.pyz \
  --repo YuriyKrasilnikov/codex-session-gc \
  --signer-workflow YuriyKrasilnikov/codex-session-gc/.github/workflows/artifacts.yml \
  --source-digest "$GC_ASSEMBLY_COMMIT" --source-ref refs/heads/main \
  --predicate-type https://slsa.dev/provenance/v1 --deny-self-hosted-runners \
  --format json
```

The verifier checks the downloaded file as the attestation subject. Stop on any
verification failure; do not execute the bootstrap. The attestation establishes
assembly origin, not cleanup correctness or a native rebuild. The exact source
commit is an input to verification, not a value inferred from the downloaded file.

Stage the release using separately trusted absolute executable paths. The command
prints an installation ID; retain that exact ID for activation and recovery.
Substitute your actual existing Codex home and receipt directory:

```sh
python3.14 -I codex-session-gc-installer-0.5.0.pyz --release v0.5.0 \
  --action stage --gh /absolute/path/to/gh --uv /absolute/path/to/uv \
  --python /absolute/path/to/python3.14
python3.14 -I codex-session-gc-installer-0.5.0.pyz --action activate \
  --installation-id INSTALLATION_UUID --home /absolute/codex-home \
  --receipt-dir /absolute/receipt-directory
python3.14 -I codex-session-gc-installer-0.5.0.pyz --action status \
  --installation-id INSTALLATION_UUID
```

Activation reports the exact `mcp_argv` for registering the server. Status can
identify a completed staged installation without an activation intent; activate
that same ID explicitly. For an existing activation intent, `--action reconcile
--installation-id INSTALLATION_UUID` records an observable terminal state without
repeating the switch. An unknown switch remains unknown; reconciliation does not
resume installation or retry it. Use the same installation directory. The
installer verifies the downloaded bundle before consuming it and does not modify
Codex configuration. A missing or insufficient verifier causes refusal. The
installer and its local executables must be trusted independently. Installation
requires network access and does not require a Rust toolchain.

For an existing installation, retain old receipt consumers and follow
[recovery and upgrades](docs/recovery.md). Consumers of the
[0.4.0 release](https://github.com/YuriyKrasilnikov/codex-session-gc/releases/tag/v0.4.0)
must retain it while their unresolved receipts require it. A version directory
already present in an application directory is not replaced. To install another
copy of 0.5.0, choose both a fresh `--install-dir` and a fresh `--bin-dir`; use
the resulting absolute CLI and reported MCP command. This does not switch a
launcher in the old application directory. See the replacement steps in
[recovery and upgrades](docs/recovery.md#replace-an-existing-050-installation).

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

## Diagnose compatibility

Observe the installed profile, supplied helper and exact database schemas before
investigating a Codex update:

```sh
codex-session-gc doctor --home /absolute/codex-home --helper /absolute/release/codex-session-gc-native
codex-session-gc doctor --home /absolute/codex-home --helper /absolute/release/codex-session-gc-native --json
```

The report shows the raw installed profile SHA-256, source identity, expected and
observed helper digests, and each declared database's schema/migration result.
An absent optional database differs from a missing authoritative state database,
a mismatch, or an unavailable observation. Exit status is 0 when the supplied
helper and present databases match, 1 for a known mismatch or missing state, and
3 for incomplete observations without a known mismatch. Omitting `--helper`
leaves that observation unknown. `--max-read-bytes` and `--timeout-seconds`
control observation bounds using the existing execution defaults.

Doctor does not change user tables or histories, copy storage, create cleanup receipts,
launch the helper, or acquire application maintenance/writer ownership. Ordinary
SQLite read transactions can create or update WAL/SHM coordination files and use
SQLite locks. Results are separate observations, not a global snapshot or
permission to delete; execution still requires native ownership and fresh checks.
The source-support claim is separate from these local observations. Producer
identity supplied on the command line remains an unverified caller observation;
an unknown running binary origin is not inferred from a version string.
By default, doctor observes the bundled support record. After an explicit
`support import`, use the returned record and context digests to compare that
record against the current profile and helper:

```sh
codex-session-gc doctor --home /absolute/codex-home \
  --helper /absolute/release/codex-session-gc-native \
  --support-install-dir /absolute/application-directory \
  --support-installation-id INSTALLATION_UUID \
  --support-record-sha256 RECORD_SHA256 --support-context-sha256 CONTEXT_SHA256 --json
```

All four support options are required together. A missing or changed selection
is reported unavailable; doctor does not substitute the bundled record. The
saved receipt records a past verification and is not fresh authentication or
cleanup authority. Local profile, helper and database observations stay separate.
See [compatibility](docs/compatibility.md) for the supported boundary.

## Browse and select

The browser opens with the largest work groups first. Works mode keeps expandable
context; Histories mode ranks matching histories across all works in a flat list.
Choose the ordering field and ascending or descending direction. Reordering keeps
selected IDs unchanged and never selects contextual parents. Each group shows its own
stored bytes, agent bytes and total across distinct physical histories, including
plain and Zstandard representations and pre/post-revert rollouts. Known observed
bytes are kept separately from an exact total: an incomplete observation shows
`≥known` and an unknown total. Sorting ranks the known observed amount; it does
not claim to order unknown actual totals. Equal known amounts put exact totals
before incomplete totals, then use the stable session ID. Wholly unknown amounts
stay last in both directions; exact zero is a value. Activity ordering also keeps
missing dates last. These are stored lengths, not promised recovered disk space. Shared providers and conditional reclaim remain unknown
until the complete cleanup preview. MAIN, ordinary agent, Guardian and
unknown source labels come from stored source metadata; a missing parent does
not make a history MAIN.

Filter by text, exact stored directory, archive state, last activity, stored-byte
bounds, history type and work group. Directory choices compare stored cwd strings
literally and case-sensitively, including punctuation and brackets; paths are not
resolved or normalized. Agents (all) includes ordinary agents and Guardians;
separate choices narrow to either source classification.
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

During execution, the terminal browser shows the operation UUID, elapsed waiting time,
the current root when known and returned root outcomes. Returned outcomes may
include refusal, partial or unknown results; they do not promise reclaimed space.
Native silence remains waiting. Progress is transient, so keep the receipt for
recovery even if the display closes.

`ui --cutoff` sets the visible initial UTC boundary. `--receipt-dir` chooses the
durable receipt directory, which must be outside the selected Codex home. Its
default is the user's state directory. The UI requires an interactive terminal.

## CLI preview and cleanup

Choose a stopped session root and an explicit UTC cutoff:

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
physical headers. Native execution holds maintenance and writer/lifecycle ownership
of the whole root and all actual spawn descendants, including unselected siblings.
A busy member refuses that work before effects. Under retained ownership and
transactions, it checks the complete closure, walks bounded namespace headers to
protect retained providers, and revalidates schema, identities and exact SQL effects.
A spawn edge marked `Open` or `Closed` is relationship metadata, not evidence of
inactivity; a status-only change does not itself refuse cleanup. Unknown status or
an ambiguous parent refuses. Related selected-thread metadata updates and
dependency-reference publication
(for example, a new fork history reference) must finish before cleanup; acquiring a
writer guard does not independently certify their completion. Selected descriptors
remain open for the checks immediately before unlink. History bodies are not
scanned or hashed.

The supported native contract is wire 6 and the exact bundled state schema
through migration 62, with the qualified companion database schemas. The same
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
The runtime boundary is the qualified local stock Codex 0.166.0 persistent public
CLI and app-server workflows, with MAv1 and MAv2 qualified separately. Custom
internal controllers and arbitrary direct ephemeral-library access are outside this
boundary. It is not a guarantee of business success or an empty runtime heap.

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

The stdio server exposes `compatibility_status`, `query_catalogue`, `session_details`, `prepare_cleanup`,
`apply_cleanup`, `cleanup_status` and `reconcile_cleanup`. Register it with absolute paths:

Use the exact `mcp_argv` returned by activation when registering the server
with `codex mcp add`. It binds the installed release, chosen home, receipt
location, application directory and installation ID; registration changes the
client configuration. `compatibility_status` uses bundled support when called
without arguments. To compare an imported record, pass both `record_sha256` and
`context_sha256` to that tool. It reads within the startup installation scope;
clients cannot choose a filesystem path or a different installation. Selecting
another imported record takes effect on the next call without restarting.

For observation only, add `--read-only` to the server arguments. This mode exposes
`compatibility_status`, `query_catalogue`, `session_details`, `prepare_cleanup` and `cleanup_status`.
Preparation returns a conditional report with `can_apply_via_mcp=false` and creates
no cleanup operation UUID, declaration or receipt. It needs no helper. Apply and
reconciliation tools are unavailable because both can write application data or
receipt evidence. Default mode retains the confirmed apply and reconciliation
tools. Read-only describes application data and receipt evidence; SQLite read
coordination may still use locks or sidecars.

Choose the home used by Codex when activating the installation. Starting the
server itself does not edit client configuration.
Reconnect the client to load changed server code. Ask for the largest project
groups, agents last active before a chosen date, or a preview for explicit IDs.
Query and details share a cached catalogue identity; explicit refresh replaces it.
`query_catalogue` accepts `cwd` for exact stored-directory equality and
`archive_state` values `archived`, `not_archived` or `unknown`. Its `scope` values
`ordinary_agents` and `guardians` narrow the existing combined `agents` scope.
These predicates combine with text, date, size and work filters on detached
metadata. `order_target=work` accepts `sort` values `total`, `own`, `agents` or
`activity`; `order_target=history` accepts `stored_bytes` or `activity`.
`direction` is `asc` or `desc`. The default is work/total/desc; invalid combinations
are refused. For the largest matching agents across works, use
`scope=agents, order_target=history, sort=stored_bytes, direction=desc`.
For the oldest matching histories in one work, combine its `roots` filter with
history/activity/asc. The response echoes `order` with `target`, `field` and
`direction`. Filtering and ordering cover the full matching history sequence before
pagination; `total_matches`, offsets and limits count histories in either mode. Contextual parents remain in group summaries and are not matching
histories unless their own facts match. Changing filters does not reread storage.
Preparation reads fresh scoped facts and returns a conditional report without a
journal or deletion. Pages contain at most 100 histories; preparation accepts at
most 200 distinct IDs per request. Larger selections can use the terminal UI.

Review the complete preparation report, then explicitly call `apply_cleanup` with
the unchanged string fields from `confirmation`: `operation_id` and `digest`.
The server retains the exact reviewed roots and candidate IDs, cutoff, startup
home identity, Guardian associations, physical manifest and qualified profile.
The client does not return filesystem integers or reconstruct the declaration.
An unclaimed confirmation expires when the server restarts; applying it then
returns unknown without dispatch. A matching saved receipt is observed only. Catalogue JSON uses
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
A duplicate apply observes the saved attempt; a different digest for a prepared
or saved operation is refused. A completed refusal before receipt creation remains
visible to apply and status in that server process. After restart, missing or
unreadable receipts mean unknown, including when an older call may still be preparing. Do not create a replacement operation to retry
an unknown deletion. Transport cancellation waits for owned backend cleanup;
receipts retain explicit unresolved lifecycle state when cleanup is unproven.

## Metadata and AI clients

Tools return metadata such as session titles, stored working directories, IDs,
timestamps, sizes, source/parent relationships and diagnostics. MCP sends these
results to its client; an AI client may include them in requests to its configured
model provider. Read-only mode limits mutations and does not redact metadata.
Do not publish raw cleanup receipts or conversation transcripts in public issues
or releases. Share only the minimal redacted diagnostic information needed.
