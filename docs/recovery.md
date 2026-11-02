# Recovery receipts

Keep the original receipt after cancellation, disconnect, partial/unknown result
or a lost reply. Do not repeat the original delete command to discover its result.

Imported support verification records are separate from cleanup receipts. Losing
or replacing a support record cannot resolve a cleanup outcome, and an unavailable
record does not select the bundled record automatically. Use the same explicit
installation, record digest and context digest when observing it again.

```console
codex-session-gc reconcile --online --receipt /absolute/cleanup.sqlite --json
```

Reconcile accepts no home, helper or selection override and never starts another
helper, sends deletion or continues undispatched work. Its scope is
`saved_journal_units`; roots skipped before receipt creation are outside that scope.
MCP uses the original operation UUID with `cleanup_status` or `reconcile_cleanup`.
A duplicate apply observes its saved attempt; a different confirmation digest
under the same UUID is refused. An unclaimed MCP reference cannot execute after
a server restart. A completed pre-dispatch refusal is retained by that process;
without a durable receipt, a restarted process reports unknown. Missing/unreadable
receipt means unknown, including when an original call might still be preparing. Do not create a new UUID to retry uncertainty.

## Receipt format and evidence

Only the current schema-2 receipt identity is supported. `recovery/journal.py`
validates its application identity, tables and event relations. Unsupported old
schemas are refused without conversion. The SQLite file is separate from source
storage; UI/MCP require its directory outside the selected home.

Exclusive creation requires an absent path with an existing parent. Start and unit
declarations use durable SQLite transactions and parent-directory synchronization.
Payloads retain their exact bytes, type/version and SHA-256. Append-only evidence
records intent before request bytes, original delivery, terminal observations and
clean shutdown separately. The receipt stores supplied evidence; its existence
alone proves neither permission nor deletion effects.

The launch ledger records intent, registration of the observed owned incarnation,
fencing and closure/absence evidence. A pending launch must be fenced or registered;
a registered launch requires explicit closure or valid absence evidence. A launch
generation remains unresolved until every member resolves. Generation-specific
lifecycle evidence never overwrites original delivery or clean-shutdown records.
Unreadable process identity is not absence. Transport cancellation waits for owned
cleanup and preserves unresolved lifecycle evidence if cleanup is unproven.

Read-only `inspect` never creates or repairs a receipt. SQLite recovery-required
state is explicit; `recover` is the separate writable operation for an already
validated supported identity. Reconciliation may append digest-bound observation
evidence to the existing receipt, preserving the original request and delivery.

## Interpreting outcomes and changing versions

Confirmed, refused, partial and unknown describe different boundaries. Original
delivery and observed final state remain distinct. `CONFIRMED_FINAL_STATE` means
selected effects were observed absent within the declared bound; it does not
attribute every effect to the original invocation or measure reclaimed space.
CLI and MCP derive the effective outcome from the same saved evidence. Missing
ownership settlement keeps it unknown. Later positive lifecycle evidence can
settle ownership without inventing an original clean-shutdown event. Reviewed
roots without a journal unit remain skipped; they are not freshly observed. A
confirmed unit alongside skipped or undispatched work keeps the operation partial.

Unknown/partial effects block dependent mutation until their safety is established;
independent read-only work may continue. There is no blind replay or whole-store
rollback.

Completed current-format records can be displayed from saved metadata after the
qualified helper changes, without decoding/replaying retired requests. Fresh
observation of unresolved selected effects requires its exact supported profile/wire
contract. Saved process ownership can be reconciled independently, including when
all units already have definitive delivery results.
Keep the prior qualified consumer and its evidence while unresolved dependencies
remain. Storage migrated beyond that consumer's schema may leave the outcome
unresolved; keeping a binary does not promise it can read newly migrated storage.
There is no fallback to a retired wire format or automatic journal migration.

## Upgrade an installed release

1. Stop submitting new cleanup calls. Let outstanding calls return, retain their
   operation UUIDs/receipt paths, and close the old UI or disconnect the old MCP
   client normally. Cancellation can still leave unknown evidence; a missing reply
   is not permission to retry.
2. List the receipt directories you actually used: explicit `--receipt` paths,
   UI/MCP `--receipt-dir`, or the default `$XDG_STATE_HOME/codex-session-gc/receipts`
   (`~/.local/state/codex-session-gc/receipts` when unset). Keep those files and the
   old installation. There is no command that discovers every receipt on disk.
3. For each unresolved receipt, use its original installed consumer:

   ```console
   /absolute/old/venv/bin/codex-session-gc reconcile --online --receipt /absolute/cleanup.sqlite --json
   ```

   Preserve partial/unknown results. Reconciliation is observation, never a resend.
   Inspect startup, launch lifecycle and unit uncertainty independently. A newer
   storage schema can refuse the old consumer; do not rewrite its profile to force
   it to proceed.
4. Use the separately verified installer to stage the new release. Retain the
   reported installation ID and explicitly activate it after checking the chosen
   home and receipt locations. `--action status` observes the completed staged
   installation or an existing activation intent. `--action reconcile` observes
   an existing activation intent and records a terminal state when established;
   it does not resume installation or retry a switch. See
   [installation](../README.md#download-and-install).
   Open its UI or run a read-only `plan` to check observation of the chosen home.

   Obtain the published 0.5.0 installer and independently verify it before
   running the following commands.

   ```sh
   python3.14 -I /absolute/verified/codex-session-gc-installer-0.5.0.pyz --release v0.5.0 --action stage \
     --gh /absolute/path/to/gh --uv /absolute/path/to/uv \
     --python /absolute/path/to/python3.14
   python3.14 -I /absolute/verified/codex-session-gc-installer-0.5.0.pyz --action activate --installation-id INSTALLATION_UUID \
     --home /absolute/codex-home --receipt-dir /absolute/receipt-directory
   python3.14 -I /absolute/verified/codex-session-gc-installer-0.5.0.pyz --action status --installation-id INSTALLATION_UUID
   ```

   The UUID in the latter commands is the ID returned by staging. If the current
   launcher is not managed by this installer, activation requires the observed
   `--expected-current /absolute/path/to/current/launcher` target. A completed
   staged installation with no activation intent can be activated by its ID. For
   an existing intent, inspect status and use the verified zipapp with `--action
   reconcile --installation-id INSTALLATION_UUID` to record an observed terminal
   state; an ambiguous switch remains unknown and is not retried. This does not
   resume an incomplete installation. Use the same explicit `--install-dir` and
   `--bin-dir` throughout if defaults are not appropriate. An installed consumer
   that provides `update` can run the same arguments through that command; 0.4.0
   does not provide it, so use the independently verified 0.5.0 installer.
5. Before replacing an MCP registration, confirm the old client has no outstanding
   cleanup calls and retain all unresolved UUIDs/receipts. Use the README registration
   command with the new Python/helper paths and the same chosen home/receipt roots;
   then reconnect the client. Re-registering does not resolve an unknown operation.
   A managed installation's reported command also fixes the support application
   directory and installation ID. Later support records can be selected by digest
   per `compatibility_status` call without another registration.
6. Remove the old installation only after no receipt depends on its qualified
   consumer. A missing configured receipt directory does not prove there are no
   receipts elsewhere.

Profile matching compares the entire saved Payload and raw contract JSON bytes,
not merely helper/schema/wire values. A change to qualification evidence, source
identity or JSON formatting can prevent a new consumer from freshly observing an
older unresolved receipt. Record validation of an unchanged execution contract
outside its raw profile. Any byte change requires preserving the named prior
consumer and resolving its receipt dependencies explicitly; do not rewrite a
saved profile to make it match.
