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

## 0.5.0 contract

The 0.5.0 contract uses wire 6 and exact state/companion schemas through state
migration 62. Its native source is pinned above with tracked storage changes:
board templates, nullable channel descriptions and attachment metadata are
included in the qualified schema. The
qualified cleanup host is Linux x86_64 on Ubuntu 26.04 LTS with glibc 2.43 and
Python 3.14.7 test oracles. This is a tested deployment, not a minimum-libc or
cross-platform guarantee. The authoritative artifact identities are in the release
manifest and the wheel's unchanged `closed_work_contract.json`.

## Updating an installed release

Use [the side-by-side upgrade procedure](recovery.md#upgrade-an-installed-release).
Download a matched bundle; changing only the helper or only a profile JSON file
is not an upgrade. Existing receipt consumers must be retained while their
outcomes remain unresolved.

The bundled scoped support claim addresses Codex 0.166.0 commit
`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856` for selected stopped work under the
conditions in [the cleanup contract](contract.md). The underlying automated
source comparison remains UNKNOWN; the scoped assessment is a supplied review
claim, not a general Rust or whole-Core equivalence proof. The installed Codex
binary has not been independently bound to that source commit.

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

Doctor also reports the bundled source-support claim separately from observed
local helper/profile/schema facts. Optional `--producer-repository`,
`--producer-commit`, `--producer-ref` and `--producer-binary-sha256` values are
caller-supplied observations; they do not prove the running binary's origin.
The profile's declared native-input digest is not an independent observation of
the source tree. MCP exposes the same read-only information through
`compatibility_status`, using the home and helper fixed at server startup.

An explicit `support import` can retrieve a specified GitHub release asset and
verify its exact bytes with the trusted installed `gh`. Supply its asset ID,
SHA-256, size and the GC assembly's full source commit. The returned record and
verification-context digests select an explicit later `support show`. A stored
receipt says verification was recorded at import time; offline display does not
reauthenticate the signature or prove the source-support claim. Neither a record
nor a matching observation authorizes cleanup. No lookup happens automatically.

For a completed installation, an explicitly selected asset can be imported and
then inspected offline. Substitute independently obtained exact identities:

```sh
codex-session-gc support import --installation-id INSTALLATION_UUID \
  --asset-id ASSET_ID --sha256 RECORD_SHA256 --size RECORD_BYTES \
  --gc-commit ASSEMBLY_COMMIT --gh /absolute/path/to/gh
codex-session-gc support show --installation-id INSTALLATION_UUID \
  --sha256 RECORD_SHA256 --context-sha256 CONTEXT_SHA256
```

`CONTEXT_SHA256` is returned by import. Pass the same explicit `--install-dir`
to both commands when the installation uses a non-default directory.

To compare the imported record with the profile and helper observed by doctor,
select it explicitly using the same installation and both digests:

```sh
codex-session-gc doctor --home /absolute/codex-home \
  --helper /absolute/release/codex-session-gc-native --json \
  --support-install-dir /absolute/application-directory \
  --support-installation-id INSTALLATION_UUID \
  --support-record-sha256 RECORD_SHA256 --support-context-sha256 CONTEXT_SHA256
```

The four support options are all required; without them doctor uses the bundled
record. If the selected record, its receipt or its installation cannot be read
and checked within the bounds, the selected record is unavailable. There is no
fallback to another record, and local compatibility observations remain visible.
The selected installation identifies where verification was recorded; the
profile and helper compared are those observed by the current doctor call. A
different running package or helper can therefore produce an identity mismatch.

For MCP, set `--support-install-dir` and `--support-installation-id` together
at server startup. Managed installation activation includes them in its
reported `mcp_argv`. Call `compatibility_status` with no arguments for bundled
support, or with both `record_sha256` and `context_sha256` for an imported
record in that fixed installation. A later call may select another imported
record without restarting. Clients cannot supply a path or installation ID to
the tool. Saved verification remains an origin record, not fresh authentication;
no record or binding comparison grants cleanup authority.

Doctor opens separate ordinary read-only SQLite transactions. It does not
change user tables or histories, copy storage, create cleanup receipts, launch the helper,
or acquire application maintenance/writer ownership. SQLite may create or update
WAL/SHM files and take normal coordination locks. Concurrent storage may change
after or between observations. Even a matching report does not validate selected
row/file effects, acquire native ownership, certify closure or authorize deletion.
An imported observation also reads the selected installation receipt, manifest,
wheel and executables. The per-file byte bound and shared cooperative deadline
apply to those reads; an interrupted wait does not guarantee that an already
running filesystem operation has stopped immediately.
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

### Compare source and prepare a scoped assessment

The comparison tool reads Git objects from an existing local repository; it does
not fetch source or authenticate an installed Codex executable. Obtain the
baseline and target commits independently, and choose boundaries that describe
the contract and its witnesses. `tools/compatibility/current-boundaries.json`
is bound to the baseline, raw profile and native-input identities it declares;
it is not a general set of exclusions for other baselines. Review and update a
separate declaration when those identities or the consumed boundaries differ.

From this checkout, set `SOURCE_REPOSITORY` and `TRUSTED_GIT` to absolute paths,
`BASELINE_COMMIT` and `TARGET_COMMIT` to full commit IDs, and `BOUNDARIES` to an
absolute declaration path. Set `PROFILE_SHA256`, `NATIVE_INPUTS_SHA256` and
`HELPER_SHA256` to the exact qualified execution identities. The following
example uses the current six source roots and explicit finite limits; revisit
the roots and bounds for the source being assessed:

```sh
compare_source() {
  PYTHONPATH=src .venv/bin/python -m tools.compatibility \
    --git "$TRUSTED_GIT" --repository "$SOURCE_REPOSITORY" \
    --baseline "$BASELINE_COMMIT" --target "$TARGET_COMMIT" \
    --boundaries "$BOUNDARIES" --profile-sha256 "$PROFILE_SHA256" \
    --native-inputs-sha256 "$NATIVE_INPUTS_SHA256" --helper-sha256 "$HELPER_SHA256" \
    --workspace codex-rs/Cargo.toml \
    --cargo-root codex-rs/state/Cargo.toml \
    --cargo-root codex-rs/thread-store/Cargo.toml \
    --cargo-root codex-rs/rollout/Cargo.toml \
    --cargo-root codex-rs/protocol/Cargo.toml \
    --cargo-root codex-rs/message-board/Cargo.toml \
    --cargo-root codex-rs/core/Cargo.toml \
    --max-cargo-nodes 12000 --max-cargo-edges 30000 --max-cargo-reads 12000 \
    --max-cargo-bytes 268435456 --cargo-seconds 180 \
    --max-objects 12000 --max-object-bytes 268435456 --object-seconds 180 \
    "$@"
}
compare_source > "$REPORT"
sha256sum "$REPORT"
```

Use a new, controlled output path for `REPORT`. The report records exact changed
objects and observed Cargo inputs, and remains conservative when a bound, input,
caller, generated source or dependency cannot be established. Review the actual
consumers and effects, including creation, fork, resume, publication and database
ownership. Give every changed object and each reported gap an evidence-backed
disposition within the stated scope; do not infer irrelevance solely from a path,
file type or a missing search hit. Keep the report, evidence and their identities
together. A successful comparison does not prove an installed binary's source.

Prepare the strict `codex-session-gc-impact-assessment-v1` JSON described by
`tools/compatibility/decision.py`, binding the exact report, commit pair, observed
source closures, helper, profile and native-input identities. Its scope, review
source, changed-object and boundary dispositions, gap dispositions and evidence
are supplied claims. Evaluate it using the same inputs and limits:

```sh
compare_source --assessment "$ASSESSMENT" > "$DECISION"
```

Use a new controlled path for `DECISION` and retain both outputs. The tool
reobserves the source and returns `unknown` if bindings or required coverage do
not match. `reviewed_same_execution` means the supplied scoped assessment says
the existing execution contract remains applicable; it does not authenticate
that review, verify the running producer, or authorize cleanup. The raw source
report can remain `UNKNOWN` while a separately reviewed scoped assessment
reaches that result. An unresolved relevant effect remains unknown or requires
review; an execution change requires the corresponding implementation and new
qualification.

### Run a bounded witness

There is no witness command-line wrapper. The maintainer API in
`tools/compatibility/witness.py` accepts a strict `WitnessSpec` and an explicit
helper and optional fixture. Declare the selected cases and report format, the
exact source-file envelope with digest and size, helper/profile/native-input
identities, script arguments, and finite `seconds`, `output_bytes`,
`scratch_bytes` and `free_floor_bytes`. The parser checks the declaration and
the runner checks its declared inputs before and after execution. The source
envelope describes the supplied files; it does not prove complete dependency
coverage or authenticate the supplied review. See
`tools/tests/test_compatibility_witness.py` for the declaration shape.

The following invokes one already prepared declaration. Set all paths to absolute
paths, `HELPER_BYTES` to the qualified helper size, and `FIXTURE` to the declared
fixture path or `-` when the declaration has no fixture. `RECEIPT` must not exist.
`SOURCE_ROOT` is this project's checkout, and `TRUSTED_PYTHON` is the Python
executable for its prepared environment:

```sh
PYTHONPATH=src .venv/bin/python - "$SPEC" "$SOURCE_ROOT" "$HELPER" \
  "$HELPER_BYTES" "$FIXTURE" "$TRUSTED_PYTHON" "$RECEIPT" <<'PY'
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

from codex_session_gc.artifacts import ArtifactIdentity
from tools.compatibility.witness import WitnessInputs, parse_spec, run_witness

spec_path, root, helper, helper_bytes, fixture, python, receipt = sys.argv[1:]
with Path(spec_path).open("rb") as source:
    spec = parse_spec(source.read(1024 * 1024 + 1))
inputs = WitnessInputs(
    Path(helper), ArtifactIdentity(spec.execution.helper_sha256, int(helper_bytes)),
    None if fixture == "-" else Path(fixture),
)
result = asyncio.run(run_witness(spec, Path(root), inputs, Path(python)))
with Path(receipt).open("x", encoding="utf-8") as output:
    json.dump(asdict(result), output, sort_keys=True)
    output.write("\n")
PY
```

The runner uses an owned disposable home and checks the expected terminal cases;
failed, missing, skipped, truncated or over-budget cases do not become positive
evidence. A receipt records the observed input and report identities, not an
authenticated source review or permission to delete. Resource and deadline
limits are cooperative around filesystem and process operations.

### Prepare a support record

Once the source assessment and relevant witnesses justify a scoped decision,
maintainers author `src/codex_session_gc/compatibility/support.json` with the
exact target source, report digest, execution identities, evidence, conditions
and any remaining gaps. The record is a separate claim: unchanged consumed
contracts can use a new support record with the existing qualified helper and
unchanged profile bytes. A changed execution contract instead needs the affected
implementation, qualification and, when necessary, a new matched release.

Validate and copy the exact source record to a new absolute output path:

```sh
PYTHONPATH=src .venv/bin/python -m tools.support_record --output "$RECORD"
```

Retain its reported SHA-256 and size. The support-only workflow can attest those
exact bytes from the trusted GC repository's `artifacts.yml` workflow on
`refs/heads/main`; its assembly commit is separate from the upstream Codex target
commit. Promote the same bytes and verify
the resulting asset before import as described above. An offline receipt records
the earlier verification; it does not reauthenticate a signature or prove that an
installed Codex binary came from the stated upstream commit.

The scope requires the actual producer and helper to use qualified matching
storage locations. Runtime admission covers local stock Codex 0.166.0 persistent
public CLI and app-server workflows, with MAv1 and MAv2 qualified separately.
The helper holds maintenance and complete root and spawn-descendant writer/lifecycle
ownership through fresh closure and effect checks. `Open` and `Closed` spawn
markers describe relationships rather than prove inactivity. Custom internal host
controllers and arbitrary direct ephemeral-library access are outside this scope.
Related selected-thread metadata updates and dependency-reference publication
(for example, a new fork history reference) must finish before cleanup; writer
acquisition does not independently certify their completion.
Unknown source variants stay protected. In particular, an Internal
Guardian source is not the qualified `SubAgentSource::Other("guardian")` case and
remains unknown/protected; this does not claim a complete Guardian lifecycle.

## Current validation boundaries

Historical native storage qualification tests used disposable LocalThreadStore producers and
independent SQL/file oracles. Positive revert cases preserve distinct logical and
immutable physical IDs; retained providers must remain readable. Compression
witnesses identify their actual stock producer and plain SQL pointer. Separate
CLI/Textual/MCP integration cases run the exact qualified helper against synthetic
homes, including mixed plain/zstd, legacy/paginated and historical representations.
They verify selected effects, retained readability and observation after restart.
Admission and effect cases using synthetic frames or mutated headers do not
establish stock producer evidence or a complete Core lifecycle guarantee.

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
missing them in polling is not proof of absence. Those historical results cover
their named storage/ownership boundaries and exact schema identities; they do not
certify every lifecycle behavior of an installed Codex process.

## Development checks

Use the project's Python 3.14 environment with the default development dependencies
and pinned build group (`uv sync --locked --group build`). Source packaging tests
import the pinned Hatch backend. Node must also be on PATH for the ordinary
JavaScript confirmation regression; it is not an application runtime dependency.
Run `python -m pytest` for the Python suite, Ruff for lint/format and mypy under the
declared project configuration.
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
derives selected files from the pinned Hatch configuration and compares complete
source-distribution and wheel membership and bytes, including explicitly generated
metadata. The assembler uses the same wheel source map. Checks also bind wheel
RECORD entries, the raw profile, legal files, bundle payloads and hash-locked
requirements to their declared source. The source-map companion is included in the
source distribution; CI entrypoints remain checkout tools.

Adding `--installed-site-packages /absolute/site-packages` compares installed
project package and metadata files with the verified wheel. Named installer
records and source-derived bytecode are classified separately; their bytes are
not claimed to equal source bytes. Generated command wrappers and other installed
distributions are outside this file comparison. These checks do not execute the
helper or qualify installed behavior; CLI, Textual and MCP require separate
behavioral gates. The complete artifact checker command is in the linked native
build instructions.

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
The installed-file gate compares project bytes against the selected source and
wheel before behavioral tests. Installed tests remove the source import path,
verify the installed module origin, and exercise doctor, CLI, Textual, MCP and
receipt observation without replay.
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
