# Durable merge recovery

A merge first validates the healthy store under its writer lock. Before changing
its log or records, it writes and fsyncs a `recovery/<sha256>.json` intent and its
containing directory. The intent commits to the proposal identity, old event
tail, event templates, every changed record/inbox file's proposed bytes, and the
current file's SHA-256 (or absence). Records also bind a canonical prior-state
payload hash so harmless differences in old JSON formatting cannot block replay. Its digest is included in each new event.
Final record `rev` and `head_event` derive from the deterministically generated
events, so arbitrary JSON fields survive without a circular hash dependency.
Deletion of record/inbox files is explicitly refused before publication.

The complete update and merge segment is published using an atomic, fsynced
replacement of `events.jsonl`, preserving the existing log bytes. This costs a
full log copy for each merge. The merge event is published with its segment;
materialization remains pending until records, state and projection agree.
Verification fails while those changes remain incomplete. Historical event
hashes are not rewritten. A partially published segment or mismatched journal
is an integrity failure, rather than something reconciliation invents or trims.

Intents remain in the store and its Git history after success. Missing or altered
published evidence fails verification. Prepared intents without published events
are inert. Verification validates identity, complete segment, paths, payloads,
latest target bytes, record/state equality and revision consistency. Later
intents supersede earlier targets for each path, allowing repeated updates of
one record. Imported v1 events before genesis retain their original accounting.

Verification and reconciliation acquire the same lock as merge publication,
seat takeovers and conflict events. Reconciliation verifies before replay, checks
all affected files before writing any, and accepts only the exact preimage or
exact final target. An unrelated current value is refused. It replays pending
files idempotently, rebuilds state and projection, verifies again, and commits
only exact validated targets, referenced intents and generated files so a new
proposal can proceed. Unknown inbox/evidence files and prepared orphan intents
remain uncommitted. An unrelated pre-staged
Git path is refused before replay, preserving its file and index entry. Interrupted replay can be
repeated without adding events. Projection errors are surfaced, never ignored.

A legacy post-genesis update with no complete recovery evidence cannot be
reconstructed from its partial event metadata. Missing/stale record rev, event
pointer or metadata, an outstanding update beyond the state revision, or a
record/state payload mismatch is an integrity failure. Reconciliation refuses
those ambiguous stores. Existing coherent legacy stores remain readable; the
format cannot prove historical arbitrary fields omitted from legacy events.
The retained evidence proves new transactions' full payloads, not authenticity
against an adversary able to rewrite all journal and event hashes together.

Crash tests use temporary synthetic stores and deliberate process exits, not
physical power loss. File and directory fsyncs establish the persistence order;
real power-loss behavior still depends on the filesystem and hardware honoring
those operations. The separate merge-CAS serialization change is needed to keep
all prepublication validation under the writer lock as well.
