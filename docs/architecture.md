# Architecture and ownership

The public adapters share a selection and execution pipeline rather than separate
cleanup implementations. [The contract](contract.md) defines observable behavior;
this page identifies the owners of that behavior in the source tree.

| Boundary | Source owner | Result and lifetime |
| --- | --- | --- |
| CLI arguments and output | `cli/main.py`, `closed_work_execution.py` | Explicit roots/subset/cutoff; one current protocol |
| Terminal browser | `cli/textual_console.py`, `console_tasks.py`, `console_display.py` | Filtered detached catalogue, reviewed intent and durable receipt path |
| MCP tools | `cli/mcp_server.py`, `execution/confirmed_operation.py`, `confirmed_declaration.py` | Cached browsing, fresh preparation and operation UUID-bound dispatch/status |
| Catalogue | `inventory/catalogue.py`, `metadata_walk.py`, `query.py` | Short read-only live-WAL transaction, then descriptor-relative metadata enumeration |
| Selection | `planning/closed_work.py`, `physical_lineage.py` | Pure explicit selection, retention and provider interpretation |
| Preparation | `execution/closed_work_facts.py`, `closed_work_request.py`, `physical_inventory.py`, `bounded_header.py` | Scoped facts, exact SQL row bounds and bounded namespace headers |
| Physical declaration | `execution/physical_manifest.py`, `parent_relation.py`, `selection_provenance.py` | Typed manifest and distinct spawn/Guardian relations |
| Dispatch and results | `execution/closed_work_apply.py`, `closed_work_operation.py`, `one_shot.py` | Exact qualified child, durable intent and unit result |
| Process ownership | `execution/launch.py`, `owned_launch.py`, `process_identity.py`, `transport.py` | Explicit child command/environment, blocked grant, observed incarnation and reap |
| Recovery | `recovery/journal.py`, `execution/closed_work_reconcile.py` | Append-only evidence and fresh observation without replay |

## Catalogue resources

A catalogue observation owns at most eight metadata workers. Workers borrow parent
directory descriptors and process at most 16 sibling names per chunk, with at most
eight outstanding chunks. Results reduce in enumeration order. Each batch settles
before recursion or parent descriptor closure; the pool joins before return.
Detached metadata supports filtering, grouping and paging without holding SQL open.
Unknown membership retains known lower bounds rather than inventing exact totals.
`CatalogueQuery` owns the conjunctive source, archive and literal-directory
predicates. Textual and MCP consume the same matching IDs from `CatalogueIndex`,
retaining complete group context and totals. Filters do not reread storage; an
explicit catalogue refresh replaces their detached snapshot.

## Confirmation and execution

Preparation is conditional. UI cancellation before dispatch joins its preview
worker. After dispatch the adapter joins the owned operation and preserves result
and receipt even if a transport response is lost. MCP fixes helper and receipt
location at startup; tools cannot substitute executable paths or receipt roots.
Textual requests a confirmation preview explicitly; ordinary observation allocates
no operation. Its worker binds the observed home identity, qualified profile and
exact selection to one operation UUID. The reviewed declaration travels unchanged
through confirmation into the shared execution mapping. Fresh execution checks
those identities and the reviewed physical/Guardian context before a receipt or
native dispatch.
MCP startup selects an explicit observation-only or cleanup capability. The same
capability governs tool registration and apply/reconciliation service entry points.
Observation-only preparation uses the plain preview path without allocating an
operation or creating receipt evidence; saved status remains readable.

The native command is launched through the packaged `owned_launch.py` gate. It
requires an exact grant within five seconds before replacing its own process.
The parent records the blocked child's observed incarnation before granting exec.
Identity includes marker, start ticks, namespaces, boot/procfs coordinate and UID.
Unreadable or changed observation coordinates are ambiguous, never proof of absence.
The launcher itself has no receipt access. The parent owns registration, fencing,
closure and child reaping; cleanup failure remains evidence alongside the primary
result. `transport.py` declares the complete child command and inherited descriptors.

Native code owns writer guards, namespace checks, database transactions and unlink
checks. Python owns admission, declaration and durable evidence; it does not
emulate native deletion. [Native build documentation](../native/README.md) describes
source reconstruction and the separate qualification boundary.

## Change boundaries

Replace a module through its typed inputs/results and observable invariants.
A schema or helper change requires exact profile/effect and recovery validation;
a catalogue-only change does not authorize a new deletion route. Keep native
producer evidence attributed to its actual source/binary. Current module exports
and CLI parser define available interfaces. Follow [the update procedure](compatibility.md)
for a Codex version change.
