# Supported cleanup contract

The supported operation removes an explicit set of closed histories while unrelated
Codex work continues. CLI, Textual and MCP use the same selection and native
execution boundary. Age is an operator-supplied filter, never evidence of closure.

## Selection and retention

The caller supplies an explicit UTC cutoff. Known last activity must be strictly
before that instant. MAIN histories and archived histories may be selected when
the qualified native contract supports their exact effects. Unknown origin, age,
archive state, missing history and ambiguous topology retain or refuse affected
work. A catalogue lifecycle label does not establish current inactivity.

A work root normally includes its spawn descendants. An explicit subset selects
only the declared histories. A retained descendant protects a cascading parent.
Guardian historical association is separate from spawn ownership: selecting a
parent does not select its Guardian, and a selected Guardian need not select,
close or even find its associated parent. Its SQL/header source and association
must agree, with no incoming spawn edge.

Physical ownership, spawn ownership, logical fork provenance and immutable
history-provider references are distinct. Retained consumers protect their required
providers, including pre-revert immutable rollout IDs. A removed consumer does
not implicitly select its provider. Logical fork provenance alone is not a
provider dependency. Unknown or conflicting relations are never repaired in storage.

## Observation and confirmation

Catalogue reads detached SQL metadata and filesystem names/stats without opening
history content. Preparation reads fresh scoped SQL effects and bounded first-line
headers throughout the managed physical namespace, including orphan consumers.
It does not copy databases or scan/hash history bodies. Independent databases
and filesystem observations are not one global atomic snapshot.

An exact stored-byte total is distinct from its observed lower bound and from
allocated or reclaimed space. Incomplete observations expose unknown totals;
conditional reclaim is a selected stored-byte estimate, never measured recovery.

Catalogue predicates apply to each history's stored source, archive state and cwd.
Exact cwd matching is literal and case-sensitive, with no path resolution or
normalization. The combined agents scope includes ordinary agents and Guardians;
narrower scopes distinguish their stored source classifications. Full work context
and its byte totals remain visible when only a child matches. A contextual parent
does not become selected automatically. Filters reuse detached metadata and keep
unknown totals distinct from known lower bounds.

Preview is read-only and non-executable. Apply observes fresh facts rather than
executing a saved preview. Textual and MCP confirmation bind the home identity,
exact qualified profile, operation UUID, cutoff, roots, selected IDs, physical
manifest and Guardian associations. Textual carries the declaration from its
reviewed preview through literal `DELETE`; ordinary read-only previews allocate
no operation. Changed home/profile identities or Guardian associations refuse
dispatch. A declaration digest does not establish human
consent; the caller must explicitly authorize cleanup. MCP returns a
`confirmation` containing only the operation UUID and digest strings. Apply takes
those two fields and resolves the immutable server-held declaration. It accepts
no full declaration. After restart an unclaimed reference cannot dispatch; a
matching saved reference observes the existing receipt only.

Direct CLI apply creates its binding from fresh facts before creating a receipt,
without an additional confirmation step or a saved-preview input. Its response
preserves the caller's original requested histories and retention reasons. The
receipt records the admitted execution scope: only the exact eligible candidates
enter its selected and requested execution sets. Reconciliation observes that
saved scope without repeating deletion.

MCP `--read-only` exposes browsing, conditional preparation and saved status.
Preparation allocates no cleanup UUID, declaration or receipt and returns
`can_apply_via_mcp=false`. Apply and reconciliation are refused, including direct
service calls; reconciliation can append receipt evidence. Default MCP mode keeps
both tools available. This capability prevents application data and evidence
mutations; it does not exclude SQLite's read coordination through locks or
sidecars.

## Native execution

Only the separately qualified native helper mutates session storage. It must
match the bundled binary digest, wire format, exact schemas and migrations.
Unsupported identities refuse; rebuilding does not qualify a binary. There is
no automatic migration, alternate schema or retired-wire fallback.

The manifest binds every selected representation, immutable ID, path, full stat,
header digest and independent history metadata. Current SQL pointer/provenance
must resolve to exactly one current physical member. A plain SQL pointer may
resolve to its same-ID compressed representation. Historical members keep their
own provider/role values. Duplicate representations and unresolved nonempty
namespace objects refuse. Unrelated definitely empty regular files are preserved.

Execution acquires writer/publication ownership and the maintenance lock, then
revalidates current identities and exact row effects under retained transactions.
Selected descriptors remain open through final pre-unlink checks. Retained
provider owners remain guarded. Late rows, changed pointers/stat identities and
expanded physical sets refuse before unapproved effects. Effects may narrow the
requested set; they may never widen it. No Python precheck substitutes for native
revalidation. Direct SQL deletion and ad hoc filesystem cleanup are unsupported.

Exact selected-owner state, history, queue, memory and applicable board effects
are bounded by the bundled profile. Opaque unselected values remain unchanged.
Multi-database/filesystem completion is not atomic; uncertain commit evidence
produces an unknown outcome rather than a rollback claim.

## Results and limits

Terminal-browser progress reports the operation UUID, local monotonic elapsed time and
root boundaries actually observed by Python. A returned root outcome can be
confirmed, refused, partial, unknown or not dispatched. It is not a percentage,
reclaimed-byte measurement or whole-operation success certificate. Native silence
remains waiting. Progress is volatile and cannot replace the final report or
receipt; losing its observer does not cancel or repeat execution.

Confirmed, refused, partial and unknown remain distinct. A confirmed unit followed
by a skipped/refused unit can produce a partial aggregate. No eligible selected
history means no helper dispatch or receipt creation. Reconciliation observes
saved launch ownership and dispatched-unit effects; it never repeats deletion or
continues undispatched work. CLI and MCP share one effective outcome projection.
Ownership can be reconciled after all unit results are definitive, without changing
original delivery or shutdown evidence. See [recovery](recovery.md).

Limits are cooperative: the defaults are 256 GiB and six hours per collection or
dispatch; requests are at most 16 MiB, decoded first-line headers at most 1 MiB
including newline, and combined helper output at most 1 MiB. SQLite deadlines do
not count exact physical I/O. A blocked filesystem call has no finite completion
guarantee. History-body integrity and a complete live Core/GuardianPool/app-server
lifecycle are outside the recorder/storage witnesses. See [compatibility](compatibility.md).
