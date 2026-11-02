#!/usr/bin/env python3
"""Generate a source-aware UV snapshot; upload only with explicit --submit."""

import argparse
import collections
import datetime
import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DETECTOR = {
    "name": "netix-uv-lock",
    "version": "0.1.0",
    "url": "https://github.com/NETIX-AI-OSS/.github/tree/main/actions/uv-dependency-submission",
}
# Changing this creates an independent snapshot stream; never include a run ID or version.
CORRELATOR = "netix-uv-lock-default"


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def encoded(value):
    return quote(value, safe="")


def package_url(package):
    """Do not turn an internal git/wheel project into a same-named PyPI project."""
    name = normalized(package["name"])
    version = package.get("version")
    source = package["source"]
    if set(source) & {"editable", "virtual", "directory"}:
        return None  # Workspace projects are roots, not registry dependencies.
    if not version:
        raise ValueError(f"Missing locked version: {name}")
    if set(source) == {"registry"}:
        if source["registry"].rstrip("/") != "https://pypi.org/simple":
            raise ValueError(f"Unsupported registry for {name}; review source identity explicitly")
        return f"pkg:pypi/{name}@{encoded(version)}"
    if set(source) == {"git"}:
        url = urlsplit(source["git"])
        if url.scheme != "https" or url.netloc.lower() != "github.com":
            raise ValueError(f"Unsupported git host/protocol for {name}")
        if not re.fullmatch(r"[a-fA-F0-9]{40}", url.fragment):
            raise ValueError(f"Missing immutable git commit for {name}")
        parts = url.path.strip("/").removesuffix(".git").split("/")
        if len(parts) != 2 or any(not part for part in parts):
            raise ValueError(f"Invalid GitHub repository for {name}")
        result = f"pkg:github/{encoded(parts[0].lower())}/{encoded(parts[1].lower())}@{url.fragment.lower()}"
        query = parse_qs(url.query)
        if set(query) - {"rev", "tag", "branch", "subdirectory"}:
            raise ValueError(f"Unrecognized git source qualifiers for {name}")
        if "subdirectory" in query:
            subdir = PurePosixPath(query["subdirectory"][0])
            if subdir.is_absolute() or ".." in subdir.parts:
                raise ValueError("Unsafe git subdirectory")
            result += "#" + quote(str(subdir), safe="/")
        return result
    if set(source) == {"url"}:
        url = urlsplit(source["url"])
        if (
            url.scheme != "https"
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(f"Unsafe direct artifact URL for {name}")
        return f"pkg:generic/{name}@{encoded(version)}?download_url={encoded(source['url'])}"
    raise ValueError(f"Unsupported locked source for {name}")


def resolve_manifest(raw, path, runtime_groups=()):
    lock = tomllib.loads(raw.decode())
    if lock.get("version") != 1 or lock.get("revision", 3) not in {3, 5}:
        raise ValueError("Unsupported UV lock format; update and test the parser first")
    packages = lock.get("package", [])
    if not packages:
        raise ValueError("Lock contains no packages")
    urls = [package_url(p) for p in packages]
    by_name = collections.defaultdict(list)
    for index, package in enumerate(packages):
        by_name[normalized(package["name"])].append(index)

    def targets(edge):
        matches = [
            i
            for i in by_name[normalized(edge["name"])]
            if ("version" not in edge or packages[i].get("version") == edge["version"])
            and ("source" not in edge or packages[i]["source"] == edge["source"])
        ]
        if not matches:
            raise ValueError(f"Unresolved locked edge: {edge['name']}")
        # Markers are intentionally not evaluated on the host: retain every locked platform variant.
        return matches

    def edges(index, extras=None, all_extras=False):
        package = packages[index]
        result = list(package.get("dependencies", []))
        for extra, children in package.get("optional-dependencies", {}).items():
            if all_extras or extra in (extras or ()):
                result.extend(children)
        return result

    roots = [i for i, url in enumerate(urls) if url is None]
    if not roots:
        raise ValueError("No local project roots; cannot determine direct dependencies safely")
    direct = set()
    scopes = collections.defaultdict(set)
    seen = set()
    queue = collections.deque()

    def enqueue(edge, scope, is_direct=False):
        for target in targets(edge):
            if is_direct:
                direct.add(target)
            queue.append((target, scope, tuple(sorted(edge.get("extra", [])))))

    for root in roots:
        for edge in edges(root, all_extras=True):
            enqueue(edge, "runtime", True)
        for group, children in packages[root].get("dev-dependencies", {}).items():
            scope = "runtime" if group in runtime_groups else "development"
            for edge in children:
                enqueue(edge, scope, True)
    while queue:
        index, scope, extras = queue.popleft()
        state = (index, scope, extras)
        if state in seen:
            continue
        seen.add(state)
        scopes[index].add(scope)
        for edge in edges(index, extras):
            enqueue(edge, scope)

    # The snapshot represents the entire universal lock, including inactive platform/group variants.
    # Keep optional edges in the union graph, but use requested extras for scope propagation above.
    def child_urls(index, visiting=frozenset()):
        if index in visiting:
            return set()
        children = set()
        for edge in edges(index, all_extras=True):
            for target in targets(edge):
                if urls[target]:
                    children.add(urls[target])
                else:
                    children.update(child_urls(target, visiting | {index}))
        return children

    resolved = {}
    for index, package in enumerate(packages):
        url = urls[index]
        if url is None:
            continue
        scope = "runtime" if "runtime" in scopes[index] or not scopes[index] else "development"
        entry = {
            "package_url": url,
            "relationship": "direct" if index in direct else "indirect",
            "scope": scope,
            "dependencies": sorted(child_urls(index) - {url}),
            "metadata": {
                "locked_name": package["name"],
                "locked_version": package["version"],
                "scope_basis": "reachable"
                if scopes[index]
                else "unreachable-lock-conservative-runtime",
            },
        }
        if url in resolved:
            prior = resolved[url]
            prior["dependencies"] = sorted(set(prior["dependencies"]) | set(entry["dependencies"]))
            if scope == "runtime":
                prior["scope"] = scope
            if entry["relationship"] == "direct":
                prior["relationship"] = "direct"
        else:
            resolved[url] = entry
    for entry in resolved.values():
        if not set(entry["dependencies"]).issubset(resolved):
            raise ValueError("Graph contains a dangling dependency")
    return {
        "name": path,
        "file": {"source_location": path},
        "metadata": {
            "lock_sha256": hashlib.sha256(raw).hexdigest(),
            "platforms": "all-locked",
            "runtime_groups": ",".join(sorted(runtime_groups)),
        },
        "resolved": dict(sorted(resolved.items())),
    }


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def api_request(repository, suffix="", token=None, payload=None):
    # Restrict credentials to the supported GitHub API host; no user-controlled base URL or redirects.
    url = f"https://api.github.com/repos/{repository}{suffix}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("API redirects require explicit repository identity review")

    with build_opener(NoRedirect()).open(
        Request(url, headers=headers, data=data), timeout=30
    ) as response:
        if payload is not None and response.status != 201:
            raise ValueError("Dependency snapshot API did not return Created")
        return json.load(response)


def api_get(repository, suffix="", token=None):
    return api_request(repository, suffix, token)


def api_post_snapshot(repository, payload, token):
    return api_request(repository, "/dependency-graph/snapshots", token, payload)


def guard(repository, sha, ref, event, live_repo, live_head, checkout_head):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identity")
    if live_repo["full_name"].lower() != repository.lower():
        raise ValueError("Repository identity mismatch")
    if event not in {"push", "schedule", "workflow_dispatch"}:
        raise ValueError("Only trusted default-branch events are allowed")
    if ref != "refs/heads/" + live_repo["default_branch"]:
        raise ValueError("Snapshot must target the live default branch")
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or sha != checkout_head or sha != live_head:
        raise ValueError("Snapshot SHA, checkout HEAD and live default HEAD must match")


def generate(root, repository, sha, ref, job_id, job_url, runtime_groups=(), scanned=None):
    paths = git(root, "ls-tree", "-r", "--name-only", sha).decode().splitlines()
    locks = sorted(p for p in paths if PurePosixPath(p).name == "uv.lock")
    manifests = {
        path: resolve_manifest(git(root, "show", f"{sha}:{path}"), path, runtime_groups)
        for path in locks
    }
    return {
        "version": 0,
        "sha": sha,
        "ref": ref,
        "job": {"id": job_id, "correlator": CORRELATOR, "html_url": job_url},
        "detector": DETECTOR,
        "scanned": scanned or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "metadata": {"repository": repository},
        "manifests": manifests,
    }


def verify_live(root, env, getter=api_get):
    repository, sha, ref = (env[key] for key in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identity")
    # Metadata is re-fetched on every verification; the default branch can change while queued.
    live_repo = getter(repository, token=env.get("GITHUB_TOKEN"))
    head = getter(
        repository, "/commits/" + encoded(live_repo["default_branch"]), env.get("GITHUB_TOKEN")
    )["sha"]
    guard(
        repository,
        sha,
        ref,
        env["GITHUB_EVENT_NAME"],
        live_repo,
        head,
        git(root, "rev-parse", "HEAD").decode().strip(),
    )


def publish(
    payload,
    root,
    env,
    runtime_groups=(),
    receipt_path=None,
    getter=api_get,
    poster=api_post_snapshot,
):
    """Rebuild payload from immutable objects, then verify live state immediately before POST."""
    repository, sha, ref = (env[key] for key in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF"))
    if not env.get("GITHUB_TOKEN"):
        raise ValueError("A repository token is required for publishing")
    if (
        payload.get("sha") != sha
        or payload.get("ref") != ref
        or payload.get("metadata", {}).get("repository") != repository
    ):
        raise ValueError("Payload repository, SHA and ref must match this trusted run")
    expected = generate(
        root,
        repository,
        sha,
        ref,
        env["GITHUB_RUN_ID"] + ":" + env["GITHUB_RUN_ATTEMPT"],
        f"https://github.com/{repository}/actions/runs/{env['GITHUB_RUN_ID']}",
        runtime_groups,
        scanned=payload["scanned"],
    )
    if payload != expected:
        raise ValueError("Payload differs from this trusted run's committed lock graph")
    verify_live(root, env, getter)
    response = poster(repository, payload, env["GITHUB_TOKEN"])
    if receipt_path is not None:
        receipt_path.write_text(
            json.dumps(
                {
                    "response": response,
                    "repository": repository,
                    "sha": sha,
                    "ref": ref,
                    "scanned": payload["scanned"],
                    "manifest_hashes": {
                        k: v["metadata"]["lock_sha256"] for k, v in payload["manifests"].items()
                    },
                },
                indent=2,
            )
            + "\n"
        )
    # No API compare-and-submit operation exists. Surface a concurrent default-branch push clearly.
    verify_live(root, env, getter)
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-groups", default="")
    parser.add_argument(
        "--submit", action="store_true", help="Publish using the current repository Actions token"
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    env = os.environ
    repository, sha, ref = (env[key] for key in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF"))
    runtime_groups = tuple(filter(None, map(str.strip, args.runtime_groups.split(","))))
    verify_live(args.root, env)
    payload = generate(
        args.root,
        repository,
        sha,
        ref,
        env["GITHUB_RUN_ID"] + ":" + env["GITHUB_RUN_ATTEMPT"],
        f"https://github.com/{repository}/actions/runs/{env['GITHUB_RUN_ID']}",
        runtime_groups,
    )
    verify_live(args.root, env)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    if args.submit:
        if args.receipt is None:
            parser.error("--submit requires --receipt")
        response = publish(payload, args.root, env, runtime_groups, args.receipt)
        print(f"Published snapshot {response['id']} for exact default HEAD {sha}")
        if env.get("GITHUB_STEP_SUMMARY"):
            with Path(env["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write(
                    f"UV snapshot `{response['id']}` for `{repository}` at `{sha}`\n\n"
                    f"Scanned: {payload['scanned']}; manifests: {len(payload['manifests'])}. "
                    "Receipt artifact records each manifest SHA-256.\n"
                )
    else:
        print(
            f"Generated {len(payload['manifests'])} manifests for exact default HEAD {sha}; nothing uploaded"
        )


if __name__ == "__main__":
    main()
