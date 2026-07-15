# AI file-version recovery

suyo creates a durable, checksum-verified snapshot before an Agent overwrites,
edits, deletes, moves, or restores an existing workspace file through the
`write`, `edit`, `apply_patch`, declarative `office`, Linux `bash`, or Linux
`code_execute` tools. New files do not need a snapshot.

## Storage and trust boundary

Snapshots are stored below the application-private data root:

```text
<private-data>/file-versions/<sha256(canonical-workspace)>/
  manifest-v1.json
  objects/<content-sha256>.blob
```

The blobs are outside the selected workspace. This is intentional: code or
shell execution that is approved for a workspace cannot silently rewrite its
recovery history. Those processes receive a writable application-private copy
mounted at the same logical workspace path, never a writable bind of the real
workspace. A non-zero exit, timeout, cancellation, unsafe symbolic link, size
limit, versioning failure, or commit conflict discards the copy. Only a clean
exit can enter the version-and-atomic-install commit path. The manifest stores
workspace-relative target paths and the canonical workspace identity; a
version from one workspace cannot be restored through a different workspace.

Before the first real-workspace replacement in a multi-file command commit,
suyo fsyncs a private recovery journal containing only relative paths,
before/after checksums, modes, and pinned version IDs. If the backend exits
mid-commit, the next startup validates every observed path and rolls the batch
back before any provider, scheduler, connector, or Agent writer starts. A path
changed by the user after the crash is never overwritten; recovery fails closed
and retains the journal and snapshots for inspection.

Commit and recovery filesystem operations are anchored to an already-opened
workspace/root and parent directory descriptor. Existing destinations are
installed with an atomic exchange and the exact displaced inode is checked;
new destinations and deletions use no-replace renames. A concurrent edit seen
at any linearization point is preserved under either the destination or a
reported conflict-temporary name and turns the transaction into a conflict;
rollback never performs a second exchange after detecting a mismatch. Journals
also pin the workspace device/inode identity, not only its canonical path.

An inode that has ever occupied a visible destination is never automatically
unlinked by transaction finalization or startup recovery. Another process may
still hold an open file descriptor and write to that old inode after the atomic
exchange; unlinking the hidden name would make those later bytes unreachable.
suyo therefore keeps such objects as explicit recovery sidecars next to the
target, normally named `.filename.suyo-tx-*` for command transactions or
`.filename.<id>.rollback.tmp` (`.rollback-backup` on Windows) for guarded
version restores. Successful command results expose their absolute paths in
both `recovery_sidecars` and
`recovery_files` metadata, conflicts include the paths in the error, and crash
recovery logs every sidecar it finds. A prepared journal cannot reliably prove
that a temporary was never published, so startup preserves it conservatively.

This is an intentional v1 storage/UX tradeoff: hidden recovery files can
accumulate inside the workspace and are not covered by version-blob retention.
There is no automatic sidecar garbage collector in v1. They should be inspected
and archived or removed only after every process that could retain an old file
descriptor has stopped. Pure preparation temporaries that are provably never
published may still be removed automatically.

Rollback of a removed empty directory is similarly no-replace: suyo prepares a
new directory with the original mode and atomically installs it only if the name
is absent. A directory recreated by the user causes a conflict and is never
chmodded. Crash recovery leaves an existing directory untouched when its entry
already matches the journal.

Each snapshot records its SHA-256, byte length, original permission mode,
timestamp, operation, and originating session/message/tool-call identifiers.
Restore verifies the blob checksum before touching the current file, snapshots
the displaced current contents, writes a same-filesystem temporary file, fsyncs
it, and installs it with an atomic replace.

## Limits

The v1 defaults are deliberately bounded:

- 100 MiB maximum for one versioned file. A larger existing file fails closed
  instead of being overwritten without recovery.
- 512 MiB of unique retained content per workspace.
- 50 versions per file.
- 2,000 version records per workspace.

Linux command transactions additionally reject workspaces above 512 MiB, any
single regular file above 100 MiB, more than 50,000 entries, and device/FIFO/
socket nodes. Existing symbolic links can be read, but a command may not mutate
or delete the link itself; a newly created link must resolve inside the selected
workspace. These checks happen in staging, so rejection leaves the real
workspace unchanged.

Hard-link topology is also fail-closed: the private command view preserves
existing in-workspace groups, but v1 rejects a command that changes a multiply
linked file, creates/breaks a hard-link group, or would otherwise require
silently splitting one inode into independent destination files. The exchange/
rename linearization point rechecks link count as well. v1 also rejects deletion
of a directory that contained any baseline descendants; recursive directory
delete will not be partially emulated by file-by-file rollback.

Content-addressed blobs deduplicate identical versions. Retention keeps the
newest versions and removes objects no longer referenced by the committed
manifest.

## Agent tools

`file_versions` is read-only. It lists all versions in the current workspace,
or filters by `file_path`, and returns stable version IDs plus checksums.

`restore_file_version` restores one listed ID to its original path. It is a
file mutation and therefore follows the same approval preset as write/edit/
patch operations. Plan mode cannot call it.

## Local API

The authenticated desktop API derives the workspace from the persisted
session; clients cannot supply an arbitrary workspace path.

```http
GET /api/file-versions?session_id=<id>&file_path=<optional>&limit=100
POST /api/file-versions/<version-id>/restore
Content-Type: application/json

{"session_id":"<id>"}
```

The restore response includes both the restored version and the recovery
version created for the displaced contents, so a restore can itself be undone.

## Scope

This boundary covers the native Agent text mutation tools, the bounded
cross-platform Office writer, image generation output, and approved Linux
shell/Python execution. Multi-file command output is first prepared in full;
each destination file is then installed atomically, with a retention-pinned
snapshot for every displaced regular file. If a later install fails, already
installed paths are rolled back only while their current bytes still match this
transaction's output. Direct edits in external applications are not part of the
Agent transaction, but a detected race is preserved and reported as a conflict.

Any new binary or domain-specific writer added to suyo must call
`FileVersionStore` before replacing an existing file and must use an atomic
install.
