# Compatibility and Codex updates

Support is bound to the bundled qualified helper digest, wire version, exact state
and companion database schemas and consumed storage semantics. A Codex version
string alone neither proves nor disproves cleanup compatibility. The current
resource is `src/codex_session_gc/execution/closed_work_contract.json`; a release
manifest must bind that resource's wheel to the delivered native binary.

The pinned native source is OpenAI Codex commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d` plus the tracked patches. Wire 6 is the
current request format. Qualification does not turn the helper into the installed
Codex executable or establish a complete live Core/app-server lifecycle guarantee.

## Current release

Release 0.2.0 uses wire 6 and exact state/companion schemas. Its native source is
pinned above with the official 0.164 board SQL changes backported: board templates
and nullable channel descriptions are included in the qualified schema. The
qualified cleanup host is Linux x86_64 on Ubuntu 26.04 LTS with glibc 2.43 and
Python 3.14.7 test oracles. This is a tested deployment, not a minimum-libc or
cross-platform guarantee. The authoritative artifact identities are in the release
manifest and the wheel's unchanged `closed_work_contract.json`.

## Updating an installed release

Use [the side-by-side upgrade procedure](recovery.md#upgrade-an-installed-release).
Download a matched bundle; changing only the helper or only a profile JSON file
is not an upgrade. Existing receipt consumers must be retained while their
outcomes remain unresolved.

## Read-only compatibility observations

`doctor --home /absolute/codex-home --helper /absolute/release/codex-session-gc-native`
reports the installed raw profile hash and source identity, the supplied helper's
expected/observed digest, and exact schema/migration matches for each database
named by that profile. Add `--json` for structured observations. The schema
comparator is shared with cleanup preparation and reconciliation; the catalogue's
weaker advisory column/foreign-key check remains distinct.

A loaded profile describes the installed package's existing qualification; loading
it does not qualify new bytes. The helper must be a regular executable with the
exact declared digest and unchanged observed identity. Missing optional databases
stay absent; missing authoritative state, exact-schema mismatch and unknown
observations have separate results. Unreadable/corrupt storage, unsafe paths,
exceeded observation bounds and a missing helper are unknown, not compatibility
proof. Use `--max-read-bytes` and `--timeout-seconds` for the existing resource
bounds; these do not establish a whole-home snapshot.

The command opens separate ordinary read-only SQLite transactions. It does not
change user tables or histories, copy storage, create receipts, launch the helper,
or acquire application maintenance/writer ownership. SQLite may create or update
WAL/SHM files and take normal coordination locks. Concurrent storage may change
after or between observations. Even a matching report does not validate selected
row/file effects, acquire native ownership, certify closure or authorize deletion.
Follow the qualification procedure below when an execution contract changes.

## Maintaining compatibility after a Codex change

1. Record `codex --version`, the installed executable identity and the exact storage
   schemas/migrations. Read only the necessary schema and metadata from real
   storage; use disposable homes for mutable experiments.
2. Compare consumed storage/source changes with the qualified contract. Trace
   affected board/state/history schema, serialization, compression/materialization,
   revert/provider identity, writer locks and transaction effects through observation,
   native execution and reconciliation. A read-only `plan` is conditional output,
   not a complete schema or lifecycle qualification certificate.
3. Reuse producer witnesses only when the consumed APIs and serialized fields are
   unchanged. Add behavioral cases for affected boundaries. A new Codex version
   alone does not require rebuilding all native source.
4. Reconstruct and build a separate helper candidate when execution changes require
   it; follow [native build and qualification](../native/README.md). Run the native
   contract/physical cases and affected Python tests, preserving the exact input,
   toolchain, artifact, outcome and scope of each observation.
5. Authorize a new helper digest/schema in the bundled contract only after its
   qualification is complete. Then build a wheel from the source distribution and
   assemble a matched bundle using the documented release command. Validate the
   installed CLI/UI/MCP outside the checkout before publishing the new artifacts.
6. Keep validation of an unchanged execution contract outside its raw JSON profile.
   Follow the byte-identity and receipt transition below whenever the profile changes.

## Current validation boundaries

Native storage tests use disposable LocalThreadStore producers and independent
SQL/file oracles. Positive revert cases preserve distinct logical and immutable
physical IDs; retained providers must remain readable. Compression witnesses
identify their actual stock producer and plain SQL pointer. Synthetic zstd frames
and header mutations test admission only; they are not stock producer evidence.

Nonempty `.rollout-materialize-*` objects must refuse observation/cleanup rather
than become selected histories. Unrelated empty regular objects remain untouched.
Changed stat/inode/pointer identities and competing representations must refuse
without cleanup effects. Held maintenance/writer locks and busy SQL transactions
exercise ownership/refusal boundaries. Controlled scheduling tests establish only
their named helper/storage invariants, not the whole upstream cancellation lifecycle.

Installed 0.164 protocol schema export confirms `thread/start`, `thread/read`,
`thread/fork`, `thread/revert`, `thread/unsubscribe` and experimental
`rollout/compress` (null parameters). Schema presence is not a producer/lifecycle
witness. Background compression replies do not prove worker drain. Short-lived
materialization files require deterministic controlled tests or source exclusion;
missing them in polling is not proof of absence. The release qualification covers
the named storage/ownership boundaries and exact schema identities; it does not
certify every lifecycle behavior of an installed Codex process.

## Development checks

Use the project's Python 3.14 environment. Run `python -m pytest` for the Python
suite, Ruff for lint/format and mypy under the declared project configuration.
Native scripts require explicit `GC_NATIVE_BINARY` and `GC_NATIVE_FIXTURE` paths
for the matching candidate; fixture-feature executables never enter the release.
Use an explicit disposable `--basetemp` for pytest. Schema/digest admission and
native ownership/effect validation remain enforced execution boundaries.

## Package construction and CI acceptance

The Python build backend is pinned in `pyproject.toml`; the `build` dependency
group and `uv.lock` fix its environment. Prepare it with
`uv sync --locked --group build --no-install-project --no-build --no-python-downloads`,
then build the sdist and build the wheel from that sdist using
`uv build --no-build-isolation`. The complete commands are in
[native/README.md](../native/README.md#build-a-matched-python-release).
Build dependencies are excluded from the bundle's runtime requirements.

The release structure checker (`python -m tools.ci_release` from a checkout)
checks wheel RECORD entries, source metadata, the raw profile, declared legal
files and every assembled bundle payload hash. These checks do not execute the
helper or qualify installed interfaces. CI tools are checkout tools, not part of
the supported installed application or source-distribution test interface.

Native reuse is admitted only when the checkout's complete declared native input
set, raw profile and downloaded qualified release artifact match their pinned
identities. A changed native input or profile refuses reuse; it requires separate
candidate qualification rather than a newly calculated digest being accepted
automatically. Package structure, native execution and installed interface checks
remain separate acceptance boundaries.

The workflows use the managed `ubuntu-26.04` x86-64 runner with pinned actions,
uv 0.12.8 and Python 3.14.7. They check runner identity and git/curl availability
before source acquisition, and record the actual image version, kernel, libc,
Python, uv, checkout commit and lock identity. The hosted image label is mutable;
these records identify a run rather than promise an immutable operating system.
The artifact lane refuses an unsupported ABI instead of selecting another helper.

The source lane imports from the checkout's `src` directory and evaluates Ruff,
mypy and the Python test partition. The required native lane supplies the exact
qualified helper to `test_mcp_native.py`, `test_mcp_disconnect.py` and
`test_mixed_native.py`; those modules are explicitly excluded from the source
lane. The CI pytest observer makes skipped cases fail their declared partition.
Rust qualification fixtures are not rebuilt or rerun for Python-only changes.

The artifact lane builds the sdist, builds its wheel, checks the matched bundle,
and installs that bundle into a separate environment outside the checkout.
Installed tests remove the source import path, verify the installed module origin,
and exercise doctor, CLI, Textual, MCP and receipt observation without replay.
Each run uses disposable test homes and temporary caches, bounded job timeouts,
read-only repository permissions and short-lived result artifacts. It does not
publish a release. A successful hosted run qualifies only its recorded source,
artifacts and named interfaces; local container compatibility probes do not
substitute for that run or establish the full upstream application lifecycle.

## Profile identity and receipt consumers

The saved qualified profile is the entire Payload, including the raw contract JSON
bytes. Editing `qualification_evidence`, `source_identity` or JSON formatting can
prevent fresh observation of older unresolved receipts even when helper bytes,
schemas and wire version remain unchanged. Keep new-version validation evidence
outside an unchanged raw profile when no execution-contract change is required.
Any raw profile byte change requires the named-receipt consumer transition above;
semantic JSON equivalence is insufficient. There is no profile rewrite or migrator.
