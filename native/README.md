# Native helper build

The helper links a patched Codex ThreadStore as a separate executable. It does
not replace the running Codex executable or install a background service. Its
intended operation is bounded cleanup of a selected closed MAIN thread and its
agents while unrelated Codex work continues.

The source inputs are `helper/` (including `Cargo.lock`), the patch files in
`patches/`, and the upstream archive obtained using `build-inputs/provenance.json`.
The archive is cached locally as `build-inputs/codex-092d3acd.tar.gz` and is
not included in the tracked delivery inputs. It contains upstream
Codex commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`, from
<https://github.com/openai/codex>. Its SHA-256 is
`79c8ea8547e1dd9969f63ef6b76a4fb0d1a024ef36c0c627fbcd2ffb658d9312`.
`build-inputs/provenance.json` records upstream source hashes before patch
application, the exact current helper and patch inputs, compiler settings and
artifact-bound qualification. The production executable below was rebuilt from
those current inputs with Rust and C/C++ source-path remapping.
The source base remains that commit. `patches/board-schema-0164.patch` carries the
narrow official Codex 0.164 board SQL changes into its existing board extension;
associated pool/lifecycle access preserves the bounded cleanup transaction path.

Run the following from the repository root in a fresh checkout. GNU tar,
GNU patch, SHA-256 tools, Python 3.14, curl, Rust 1.99.1 with Cargo, and a native C/C++ build
toolchain are required. Cargo dependencies include Git sources and bundled
SQLite; obtaining a missing archive and the first dependency fetch need network access.
The pinned upstream declares Rust 1.95.0; this release uses the explicit Rust
1.99.1 toolchain. This recipe reconstructs source and
build settings; it does not establish byte-for-byte reproducible binaries
across machines or toolchains.

```sh
set -eu
if [ ! -e native/build-inputs/codex-092d3acd.tar.gz ]; then
  native_archive_url="$(python3 -c 'import json; from pathlib import Path; print(json.loads(Path("native/build-inputs/provenance.json").read_text())["archive_url"])')"
  curl --fail --location --output native/build-inputs/codex-092d3acd.tar.gz \
    -- "$native_archive_url"
fi
printf '%s  %s\n' \
  79c8ea8547e1dd9969f63ef6b76a4fb0d1a024ef36c0c627fbcd2ffb658d9312 \
  native/build-inputs/codex-092d3acd.tar.gz | sha256sum --check --strict
# Refuse to overwrite an existing source tree.
test ! -e native/upstream
mkdir native/upstream
tar -xzf native/build-inputs/codex-092d3acd.tar.gz \
  --strip-components=1 -C native/upstream
for native_patch in native/patches/*.patch; do
  patch --batch --forward --fuzz=0 -p1 -d native/upstream < "$native_patch"
done
```

If the host dependency cache is missing, prepare it explicitly after source
reconstruction. This separate step needs network access; reuse an existing
verified cache when available:

```sh
CARGO_HOME="$PWD/native/cargo-home" cargo +1.99.1 fetch \
  --manifest-path native/helper/Cargo.toml --locked \
  --target x86_64-unknown-linux-gnu
```

Then build offline with controlled compiler inputs. An offline failure does not
automatically fetch dependencies:

```sh
python3 - <<'PY'
from pathlib import Path
import hashlib
import os
import shlex
import subprocess
import tempfile

source = Path.cwd().resolve()
cache = source / "native/cargo-home"
# Keep this build directory until the binary has been copied and qualified.
build_root = Path(tempfile.mkdtemp(prefix="codex-session-gc-build-"))
target = build_root / "target"
binary_name = "codex-session-gc-native"
features = []
mappings = sorted([
    (source, "/usr/src/codex-session-gc"),
    (cache, "/usr/src/codex-session-gc/cargo-home"),
    (build_root, "/usr/src/codex-session-gc/build-root"),
    (target, "/usr/src/codex-session-gc/build"),
], key=lambda pair: len(str(pair[0])))
# Preserve the existing home and toolchain selection; discard ambient build flags,
# wrappers, target overrides and optional build metadata.
environment = {key: os.environ[key] for key in ("PATH", "HOME", "RUSTUP_HOME")
               if key in os.environ}
environment.update(
    CARGO_HOME=str(cache), CARGO_TARGET_DIR=str(target),
    CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_DEV_INCREMENTAL="false",
    CARGO_INCREMENTAL="0", CC="cc", CXX="c++",
    CARGO_ENCODED_RUSTFLAGS="\x1f".join(
        f"--remap-path-prefix={old}={new}" for old, new in mappings),
    CFLAGS=shlex.join(f"-ffile-prefix-map={old}={new}" for old, new in mappings),
    CXXFLAGS=shlex.join(f"-ffile-prefix-map={old}={new}" for old, new in mappings),
)
subprocess.run([
    "cargo", "+1.99.1", "build", "--manifest-path",
    str(source / "native/helper/Cargo.toml"), "--locked", "--offline",
    "--jobs", "2", "--no-default-features",
    *(["--features", ",".join(features)] if features else []), "--bin", binary_name,
], cwd=source / "native/helper", env=environment, check=True)
binary = target / "debug" / binary_name
with binary.open("rb") as stream:
    print(hashlib.file_digest(stream, "sha256").hexdigest(), binary)
PY
```

The recipe maps source, dependency-cache and generated-output roots to stable
`/usr/src/codex-session-gc` paths. Rust source locations and C/C++ `__FILE__`
locations require their respective compiler flags. Explicit environment strings
are separate inputs: the build preserves the existing home and toolchain lookup
but discards ambient compiler flags, wrappers and optional build metadata.
Inspect the resulting ELF as well as the source; remapping is not a substitute
for artifact qualification.

Keep the default development profile and the explicit environment above when
following this recipe. Additional compiler flags, wrappers, target overrides,
or Cargo configuration can change the artifact. The `fixtures` feature and
`gc-native-fixture` executable are for isolated test data only; a build with
that feature is not a delivery artifact. Tests live in `tests/` and
`helper/tests/` and must use disposable homes, never a production Codex home.

Building the helper alone does not qualify it for cleanup. The Python adapter
loads `src/codex_session_gc/execution/closed_work_contract.json` and accepts only
its qualified production binary SHA-256, wire version, and exact schema and
migration identities. The supported production helper uses Rust 1.99.1, edition
2024, wire 6, and SHA-256
`6ca072bf606733acd180031a34a55c9c6581c4b749dd8374a7a12c81d0d4efa5`
(81,913,024 bytes). The release bundle names the executable
`codex-session-gc-native`; supply its absolute path with `--helper`.
A digest becomes selectable
only when the Python contract resource records its qualification. Source profile
qualification and installation are separate operations.

The validator accepts only the exact deployed state schema through migration 61
and the pinned exact companion database schemas. These deployed DDL and checksum
identities are qualification inputs. Different schemas, triggers or migrations
refuse cleanup; no additional schema variant is qualified.
The exact board schema includes `board_templates` and nullable
`channels.description`. Only a selected root's template rows enter its declared
board effects; selecting a child preserves its parent's template. Opaque retained
templates remain unchanged. The old board layout refuses without migration, and
an absent optional board database remains absent. Fresh and official
known-predecessor-upgraded board inputs have the same qualified schema.

A logical target saves the exact raw SQL current pointer and immutable current
rollout ID separately from its complete observed physical rollout manifest.
Each physical member binds its native filename owner and immutable ID, plain or
zstd representation, path, device/inode/size/nanosecond timestamps, decoded
first-line SHA-256, history mode, immutable history prefix and logical fork
provenance. Execution checks exactly that saved set; namespace discovery cannot
add deletion targets. Current SQL origin, provider, role, history mode and archive
state must match the current member. Historical members retain their separately
bound metadata and may have older provider or role values. A plain SQL pointer
may identify the same saved physical rollout's compressed representation.

The exhaustive tagged `parent_relation` distinguishes roots, ordinary spawn
ownership and Guardian historical association. Ordinary spawn ownership requires
the closed authoritative SQL edge; embedded source/header parents must agree.
A selected Guardian requires exact SQL/header `SubAgentSource::Other("guardian")`,
a valid association and no incoming spawn edge. Its associated parent may be
absent, unowned or active; selecting the Guardian does not acquire that parent's
writer ownership. Selecting an ordinary parent does not select its associated
Guardian.

The selected-owner row bounds include `guardian_review_feedback` and
`thread_read_receipts` for every selected ordinary or Guardian owner. Native
validation checks exact row IDs and ownership under retained transactions,
deletes only admitted rows, and preserves opaque unselected values. Unapproved
late rows refuse before effects. History-cache effects use the union of selected
logical initial IDs and exact observed selected immutable rollout IDs. Other SQL
effects remain keyed by selected logical owners. The existing four-table history
deletion API uses the retained transaction; unattributed cache keys remain
untouched. Observed retained ownership collisions and retained immutable history
prefix references protect affected keys, even when the referenced provider file
is absent. Logical fork provenance alone is not a provider dependency in either
history mode. Guardian association and spawn ownership are separate relations.

Both plain and compressed first lines have a decoded limit of 1 MiB including
the newline. Standard concatenated zstd streams use the existing native decoder
with window log maximum 21. A borrowed-descriptor adapter clamps physical reads
to 4 KiB before buffering and limits physical input to 2 MiB. Header admission
does not establish body readability, tail validity or whole-file integrity.
Cleanup does not materialize bodies or compress files. Duplicate representations,
conflicting immutable IDs and unresolved nonempty entries refuse. Definitely
empty unrelated regular files remain preserved under the existing empty-file
rules. Selected descriptors remain open through final pre-unlink checks.

A retained Guardian may keep its historical association after its ordinary
parent's deletion. Its history prefix must not require a selected provider.
Logical writer ownership is acquired once per retained owner, while every
physical member keeps its descriptor and full-stat checks through cleanup
commits. The retained state transaction must identify exactly one actual current
physical member and match that member's SQL/header provenance; a missing current
pointer cannot make all members historical. Incoming spawn edges refuse this
exception. Exact declared supplemental writer paths remain mandatory.

The closed-work boundary requires selected related fork, resume and publication
operations to finish before cleanup; unrelated work continues. Maintenance
publication exclusion, authoritative activity, exact SQL row effects, descriptor
checks and per-file/per-database outcome evidence remain enforced. Unselected
closure members stay locked; selected state rows and the work root remain
mandatory. Historical names remain untouched. Ambiguous multi-database commit
evidence reports UNKNOWN rather than claiming rollback.

Qualification receipts remain attributed to the actual source and binary tested.
The production artifact above passed 62 current-contract cases, 24 physical
storage cases, and two reconstructed fresh/official-known-predecessor-upgrade board witnesses.
These are native recorder/storage ownership, retained-history read/resume,
exact-effect and refusal observations. The qualified host is Linux x86_64,
Ubuntu 26.04 LTS, glibc 2.43, with Python 3.14.7 test oracles. Other deployment
environments require validation; these results do not establish a complete live
Core/GuardianPool/app-server lifecycle guarantee.

Producer witnesses retain their actual source/binary attribution. Reuse requires
checking that the consumed APIs and serialized fields are unchanged. A fixture
producer is a test executable and never a production delivery artifact.

Use disposable homes only. Native Python test oracles require Python 3.14's
standard `compression.zstd`. Set `GC_NATIVE_BINARY` and `GC_NATIVE_FIXTURE` to
explicit matching artifacts when invoking `native/helper/tests/closed_work_batch.py`
or `physical_rollouts.py`. Fixture-feature builds never enter a release.
See [the compatibility procedure](../docs/compatibility.md) for deciding which
checks and producer witnesses a Codex update actually changes.

The journal envelope identity, including its historical MIME label and payload
version 1, is preserved; execution accepts only wire 6. Completed old receipt
metadata remains readable without decoding or replaying a retired request.
Unresolved old-profile requests require their named qualified consumer. There is
no fallback to a retired request format or another schema. The helper may create
`.tmp` and its maintenance lock without sweeping unrelated writer locks.
Rebuilt binaries require independent qualification before the static resource
authorizes their digest.

Build output, dependency caches, reconstructed upstream sources, logs and
generated diagnostic archives are local artifacts. Keep an existing verified
source archive for reuse. The tracked delivery inputs are provenance, patches,
helper sources, lockfile, tests and this documentation; the archive can be
obtained again from its pinned URL and verified before reconstruction.

## Installation boundary

The Python wheel does not contain this executable. A qualified release supplies
it separately with its SHA-256 in the same manifest as the Python wheel and
runtime requirements. Keep it in the unpacked versioned release directory and
pass its absolute path through `ui --helper`, `mcp --helper` or
`apply --online --helper`. Preserve executable permissions. Verify both the
release manifest and the Python contract identity before use; arbitrary rebuilt
bytes are refused. Source reconstruction above is for maintainers, not a user
installation prerequisite. Update the qualified resource and release manifest
together whenever an artifact identity changes.

## Validate a changed native helper

Use the same reconstructed source and explicit toolchain settings above. To build
a separate fixture producer, rerun the Python build block with
`binary_name = "gc-native-fixture"` and `features = ["fixtures"]`. Each invocation
uses a separate build directory. Retain the printed absolute executable paths
and run the tests against the chosen production helper and fixture:

```sh
uv sync --locked
GC_NATIVE_BINARY=/absolute/qualified/codex-session-gc-native GC_NATIVE_FIXTURE=/absolute/fixture/gc-native-fixture .venv/bin/python native/helper/tests/current_contract.py
GC_NATIVE_BINARY=/absolute/qualified/codex-session-gc-native GC_NATIVE_FIXTURE=/absolute/fixture/gc-native-fixture .venv/bin/python native/helper/tests/physical_rollouts.py
```

The scripts create disposable homes and compare exact effects, retained data and
refusals. They do not authorize or edit a new profile automatically. A changed
storage contract requires corresponding observation/native-bound changes and
artifact-bound qualification evidence before the Python resource is updated.
Then run the Python suite against the coherent source/profile/helper combination.
After `uv sync --locked`, explicitly supply the qualified helper to its real MCP
and native integration cases using their separate environment variable:

```sh
GC_MCP_NATIVE_HELPER=/absolute/qualified/codex-session-gc-native .venv/bin/python -m pytest --basetemp /absolute/new-disposable-tests
```

`GC_MCP_NATIVE_HELPER` is distinct from `GC_NATIVE_BINARY`, which selects the
helper for the standalone native scripts above. Skipped real integration cases
do not qualify those interfaces. Use a new disposable `--basetemp` directory;
retain the previous qualified consumer while unresolved receipts depend on it.
See [compatibility](../docs/compatibility.md).

## Build a matched Python release

For using the project from a checkout, obtain the matching published qualified
helper from the [release bundle](../README.md#download-and-install); run the
checkout's Python entry point with that absolute helper path. Rebuilding native
source is intended for maintainers changing or validating the native contract.
A digest mismatch remains a refusal, even after a successful build.

After a candidate helper and exact bundled profile have been qualified, synchronize
the Python development environment and build the source distribution first. The
commands below assume the already-qualified release helper has been retained at
`/absolute/qualified/codex-session-gc-native` and the output directories are absent:

```sh
set -eu
uv sync --locked
test ! -e build/release-0.2.0
mkdir -p build/release-0.2.0
uv build --sdist --no-config --no-python-downloads --python .venv/bin/python --out-dir build/release-0.2.0
uv build --wheel build/release-0.2.0/codex_session_gc-0.2.0.tar.gz --no-config --no-python-downloads --python .venv/bin/python --out-dir build/release-0.2.0
.venv/bin/python tools/build_release.py --wheel build/release-0.2.0/codex_session_gc-0.2.0-py3-none-any.whl --helper /absolute/qualified/codex-session-gc-native --output build/release-0.2.0/codex-session-gc-0.2.0-linux-x86_64
```

`uv build` installs the declared build backend into an isolated build environment.
A fresh build may need network access for that backend; add `--offline` only when
its dependencies are already cached. The bundle assembler always exports runtime
requirements from the existing lock offline; `uv sync --locked` prepares its
Python tooling/dependency cache. Record the backend version from the wheel's
`WHEEL` metadata; source reconstruction does not promise identical binary bytes
on a different machine.

The assembler checks wheel version, Python/dependency metadata, exact raw profile,
helper digest and license/notice payload against the selected source. It refuses
an existing output directory. Inspect the sdist/wheel contents, the wheel RECORD
and all bundle manifest hashes before creating downloadable assets:

```sh
tar -C build/release-0.2.0 -czf build/release-0.2.0/codex-session-gc-0.2.0-linux-x86_64.tar.gz codex-session-gc-0.2.0-linux-x86_64
cd build/release-0.2.0
sha256sum codex-session-gc-0.2.0-linux-x86_64.tar.gz codex_session_gc-0.2.0-py3-none-any.whl codex_session_gc-0.2.0.tar.gz > SHA256SUMS
```

Ship those three assets and `SHA256SUMS` together with the same version/tag.
The source archive contains helper sources, patches, provenance and tests; it
contains no reconstructed upstream tree, dependency caches or production binary.
The wheel is generic Python; the matched bundle supplies the Linux helper.
Keep [Apache-2.0](../LICENSE), [attribution](../NOTICE) and the dependency notices
with the artifacts that contain their code. Installing the final bundle outside
the checkout and exercising disposable CLI/UI/MCP inputs checks the actual
published installation route.
