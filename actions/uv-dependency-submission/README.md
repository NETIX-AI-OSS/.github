# UV dependency snapshot generator

This action generates a Dependency Submission API payload from committed `uv.lock`
files. Publishing requires the explicit `submit: 'true'` input; the default only
generates a preview. Adopt the ongoing guarded caller described below before
publishing; adopting only a one-off snapshot is unsafe because
[user submissions take precedence over native graph jobs](https://docs.github.com/en/rest/dependency-graph/dependency-submission).

Python 3.11+ and git are sufficient. There is no dependency installation, UV
resolution, project import, model download, or build execution. The composite
action selects Python 3.13 and must run on a `build-only` caller job.

## Payload identity and completeness

Each tracked lock is its own manifest, keyed by its exact repository-relative
path, with that same `file.source_location`. Every locked external package and
platform/Python variant is retained. Markers are not evaluated on the Linux host.
Untracked locks and working-tree edits are ignored: contents come from git
objects at the validated commit.

| Locked source | Package identity |
| --- | --- |
| `https://pypi.org/simple` | `pkg:pypi/normalized-name@locked-version` |
| GitHub HTTPS git source | `pkg:github/owner/repository@full-locked-commit`, preserving a subdirectory |
| Direct HTTPS wheel/archive | `pkg:generic/name@version?download_url=exact-encoded-url` |
| Local workspace project | Traversed as a root, not misrepresented as a published package |

Git tags and branches are not snapshot versions: the immutable commit from the
lock is used. Internal git/wheel packages are never assigned the identity of an
unrelated same-named PyPI package. Their PyPI child dependencies are still
reported. GitHub/generic identities describe provenance; matching Python
advisories for unpublished internal projects is outside this action's coverage.
Other registries, unsupported sources, unpinned git references, missing child
nodes, rootless locks, and unknown lock versions/revisions fail the generation
instead of silently omitting dependencies. Reviewed lock revisions: 3 and 5.

Resolved keys equal PURLs, and child edges reference those same PURLs. Duplicate
PURLs merge their child edges. Direct dependencies are children of local project
roots, including root extras and dependency groups. Other dependencies are
indirect. The union graph includes locked optional edges; scope propagation uses
the extras actually requested along each root-to-package path. Runtime reachability
wins when a package is also used by development tools. Root production extras are
included; groups are development unless explicitly listed in `runtime-groups`.
For groups that ship production ML or edge artifacts, use
`runtime-groups: edge,ml`. If a locked node cannot be reached, retain it conservatively
as runtime and mark that fact in `metadata.scope_basis`.

Detector name `netix-uv-lock` and correlator `netix-uv-lock-default` are constant
for every run, attempt, action version, and trigger. The snapshot contains all
tracked locks in one stream, so different per-lock streams cannot accumulate old
versions. The job ID carries the run ID/attempt; it is not the correlator. Do not
rename the detector/correlator or add independent publishers without a migration
plan, because independent streams may merge or supersede one another.

## Exact default-branch guards

The command accepts only `push`, `schedule`, and `workflow_dispatch` events. It
reads the authoritative repository identity, default branch and current commit
from the GitHub API. Event `GITHUB_REF` must equal that live default branch, and
event `GITHUB_SHA`, local git HEAD and live default HEAD must all match. These
checks run both before and after generation. Before a POST, the publisher rebuilds
the graph from the committed objects and binds the entire payload to the current
repository, event SHA/ref and run ID/attempt. It then re-reads repository metadata
(including the current default branch) and the live default HEAD immediately
before submission, and checks them again after a successful POST. Branch dispatches and stale queued
runs fail; pull requests and `pull_request_target` cannot produce snapshots.
Repository API calls reject redirects rather than forwarding credentials.

The action is intended to live at the public
`NETIX-AI-OSS/.github/actions/uv-dependency-submission` path. Pin callers to a
reviewed **immutable commit**, not `main` or a moving tag. A public action can be
used by private consumers without sharing a private actions repository. Each
consumer's Actions allowlist must permit the shared action and the pinned
checkout/setup-python actions; the initial data-service policy permits all actions.

## Ongoing consumer callers

Thin consumer workflows should:

1. Trigger on every push to their actual default branch (`main`, or `master` for
   oBIX), a daily schedule and manual dispatch. Do not use path filters that could
   skip lock deletion or changes to the publishing workflow.
2. Use a single repository-wide concurrency group `netix-uv-lock-default` with
   `cancel-in-progress: false`, a five-minute timeout and `runs-on: build-only`.
   Pending older jobs can be superseded; a stale active job must fail its SHA guard.
3. Check out the event's exact SHA with credentials not persisted. Give only the
   publishing job `contents: write`; all other jobs retain read access. Do not run
   project install scripts or untrusted pull-request code with this permission.
4. Call this action at an immutable reviewed SHA. Use the same detector and
   correlator for push, schedule and dispatch; do not create a matrix of snapshots.
5. Immediately before a POST, the shared publisher revalidates the payload SHA/ref against
   the live default branch, revalidate checkout HEAD, and verify the submitted
   bytes are the generated payload. Reject a changed default branch or HEAD.
   It submits only to the fixed GitHub API snapshot endpoint for that repository.
6. Save the successful snapshot ID, source commit, manifest hashes and scan time
   in a job summary/artifact. Monitor failed workflows and missed daily runs; keep native
   dependency scanning enabled. Never dismiss alerts to simulate refresh.

There is no atomic GitHub compare-and-submit endpoint. A push concurrent with the
POST may briefly make a snapshot stale; serialized push jobs and the daily
fallback must replace it. The publisher checks HEAD after a successful POST
and fails visibly if it changed, leaving the next queued/scheduled
job to publish the current commit. Do not roll this out if those ongoing callers
and failure monitoring are absent. Removing the workflow alone does not remove
the higher-priority submitted snapshot; retirement needs an explicit migration.
Deleting the last UV lock generates an empty replacement in the same stream,
instead of leaving the last dependency set authoritative forever. Confirm empty
replacement behavior and alert refresh in the first controlled rollout before
expanding adoption.

The shared repository's CI only tests the generator/publisher with mocked API
operations. It never submits a snapshot. Consumer callers are reviewed separately;
no local one-off snapshots are submitted while establishing those workflows.

## Local verification

```sh
python3 -m unittest discover -s actions/uv-dependency-submission -v
```

Tests cover source identity collisions, pinned git provenance, direct/transitive
and development/runtime overlap, explicit production groups, extras, platform
version splits, graph cycles, merged nodes, conservative orphan handling, broken
edges, unsupported formats/sources, trusted event/SHA/ref guards, nested lock
paths, dirty/untracked-file exclusion and stable stream identity across runs.
Publisher tests cover payload tampering, default-branch renames, new commits,
untrusted events, missing tokens, a concurrent push during POST, receipt retention,
lock deletion and redirect refusal. Tests never make a real API POST.

The generator is also exercised without uploads against real project locks.
Full previews remain local audit artifacts and are not committed here.
