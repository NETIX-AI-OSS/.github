import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("snapshot", Path(__file__).with_name("snapshot.py"))
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)

ROOT = """version = 1
[[package]]
name = "application"
version = "1"
source = { virtual = "." }
"""


def package(name, version="1", extra=""):
    return f'''\n[[package]]
name = "{name}"
version = "{version}"
source = {{ registry = "https://pypi.org/simple" }}
{extra}
'''


def resolved(text, runtime_groups=()):
    return snapshot.resolve_manifest(text.encode(), "uv.lock", runtime_groups)["resolved"]


class IdentityTests(unittest.TestCase):
    def test_pypi_normalization(self):
        self.assertEqual(
            snapshot.package_url(
                {
                    "name": "My_Package.Name",
                    "version": "1.0+local",
                    "source": {"registry": "https://pypi.org/simple/"},
                }
            ),
            "pkg:pypi/my-package-name@1.0%2Blocal",
        )

    def test_git_uses_commit_not_project_name_or_moving_tag(self):
        source = "https://github.com/NETIX-AI-OSS/envoy-pyauth.git?rev=v4.0.0#" + "a" * 40
        self.assertEqual(
            snapshot.package_url(
                {"name": "public-name-collision", "version": "4.0.0", "source": {"git": source}}
            ),
            "pkg:github/netix-ai-oss/envoy-pyauth@" + "a" * 40,
        )

    def test_git_subdirectory_retained(self):
        result = snapshot.package_url(
            {
                "name": "sdk",
                "version": "1",
                "source": {
                    "git": "https://github.com/owner/project.git?subdirectory=sdk/python#"
                    + "b" * 40
                },
            }
        )
        self.assertTrue(result.endswith("#sdk/python"))

    def test_direct_wheel_never_claims_pypi_identity(self):
        result = snapshot.package_url(
            {
                "name": "internal-wheel",
                "version": "1",
                "source": {"url": "https://github.com/org/repo/releases/download/v1/internal.whl"},
            }
        )
        self.assertTrue(result.startswith("pkg:generic/internal-wheel@1?download_url="))
        self.assertIn("github.com%2Forg%2Frepo", result)

    def test_unreviewed_sources_fail_instead_of_silently_disappearing(self):
        for source in (
            {"registry": "https://private.example/simple"},
            {"path": "elsewhere"},
            {"git": "https://github.com/org/repo.git#main"},
            {"git": "https://other.example/org/repo.git#" + "a" * 40},
            {"url": "https://secret:password@example.org/pkg.whl"},
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                snapshot.package_url({"name": "package", "version": "1", "source": source})


class GraphTests(unittest.TestCase):
    def test_direct_transitive_and_dev_runtime_overlap(self):
        text = (
            ROOT
            + """dependencies = [{ name = "runtime-parent" }]
[package.dev-dependencies]
dev = [{ name = "dev-parent" }]
"""
            + package("runtime-parent", extra='dependencies = [{ name = "shared" }]')
        )
        text += package(
            "dev-parent", extra='dependencies = [{ name = "shared" }, { name = "dev-only" }]'
        )
        text += package("shared") + package("dev-only")
        graph = resolved(text)
        self.assertEqual(graph["pkg:pypi/runtime-parent@1"]["relationship"], "direct")
        self.assertEqual(graph["pkg:pypi/dev-parent@1"]["scope"], "development")
        self.assertEqual(graph["pkg:pypi/shared@1"]["scope"], "runtime")
        self.assertEqual(graph["pkg:pypi/shared@1"]["relationship"], "indirect")
        self.assertEqual(graph["pkg:pypi/dev-only@1"]["scope"], "development")
        self.assertEqual(graph["pkg:pypi/runtime-parent@1"]["dependencies"], ["pkg:pypi/shared@1"])

    def test_explicit_production_groups_override_development_default(self):
        text = ROOT + '[package.dev-dependencies]\nml = [{ name = "torch" }]\n' + package("torch")
        self.assertEqual(resolved(text)["pkg:pypi/torch@1"]["scope"], "development")
        self.assertEqual(resolved(text, ("ml",))["pkg:pypi/torch@1"]["scope"], "runtime")

    def test_requested_extras_propagate_scope(self):
        text = (
            ROOT
            + """dependencies = [{ name = "parent" }]
[package.dev-dependencies]
dev = [{ name = "parent", extra = ["testing"] }]
"""
            + package(
                "parent",
                extra="""[package.optional-dependencies]
testing = [{ name = "test-tool" }]
""",
            )
            + package("test-tool")
        )
        graph = resolved(text)
        self.assertEqual(graph["pkg:pypi/parent@1"]["scope"], "runtime")
        self.assertEqual(graph["pkg:pypi/test-tool@1"]["scope"], "development")
        self.assertEqual(graph["pkg:pypi/parent@1"]["dependencies"], ["pkg:pypi/test-tool@1"])

    def test_all_platform_versions_preserved_and_specific_edges_respected(self):
        text = (
            ROOT
            + """dependencies = [
  { name = "platform", version = "1", source = { registry = "https://pypi.org/simple" }, marker = "sys_platform == 'linux'" },
  { name = "platform", version = "2", source = { registry = "https://pypi.org/simple" }, marker = "sys_platform == 'win32'" },
]
"""
            + package("platform", "1", 'dependencies = [{ name = "old-child" }]')
        )
        text += package("platform", "2", 'dependencies = [{ name = "new-child" }]')
        text += package("old-child") + package("new-child")
        graph = resolved(text)
        self.assertEqual(graph["pkg:pypi/platform@1"]["dependencies"], ["pkg:pypi/old-child@1"])
        self.assertEqual(graph["pkg:pypi/platform@2"]["dependencies"], ["pkg:pypi/new-child@1"])
        self.assertEqual(len(graph), 4)

    def test_cycles_terminate_and_edges_remain_closed(self):
        text = ROOT + 'dependencies = [{ name = "one" }]\n'
        text += package("one", extra='dependencies = [{ name = "two" }]')
        text += package("two", extra='dependencies = [{ name = "one" }]')
        graph = resolved(text)
        self.assertEqual(graph["pkg:pypi/two@1"]["dependencies"], ["pkg:pypi/one@1"])

    def test_same_purl_variants_merge_children(self):
        text = ROOT + 'dependencies = [{ name = "same" }]\n'
        text += package("same", extra='dependencies = [{ name = "one" }]')
        text += (
            package("same", extra='dependencies = [{ name = "two" }]')
            + package("one")
            + package("two")
        )
        self.assertEqual(
            resolved(text)["pkg:pypi/same@1"]["dependencies"], ["pkg:pypi/one@1", "pkg:pypi/two@1"]
        )

    def test_unreachable_locked_variant_is_preserved_conservatively(self):
        entry = resolved(ROOT + package("unused"))["pkg:pypi/unused@1"]
        self.assertEqual(entry["scope"], "runtime")
        self.assertIn("conservative", entry["metadata"]["scope_basis"])

    def test_fail_on_unknown_format_missing_target_and_no_root(self):
        for text in (
            ROOT.replace("version = 1", "version = 2", 1),
            ROOT + 'dependencies = [{ name = "missing" }]\n',
            "version = 1\n" + package("only-registry"),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                resolved(text)


class GuardTests(unittest.TestCase):
    def test_only_exact_live_default_branch_from_trusted_event(self):
        good = {
            "repository": "org/repo",
            "sha": "a" * 40,
            "ref": "refs/heads/main",
            "event": "push",
            "live_repo": {"full_name": "org/repo", "default_branch": "main"},
            "live_head": "a" * 40,
            "checkout_head": "a" * 40,
        }
        snapshot.guard(**good)
        for field, value in (
            ("event", "pull_request"),
            ("event", "pull_request_target"),
            ("ref", "refs/heads/feature"),
            ("live_head", "b" * 40),
            ("checkout_head", "b" * 40),
            ("repository", "attacker/fork"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                snapshot.guard(**{**good, field: value})
        for event in ("schedule", "workflow_dispatch"):
            snapshot.guard(**{**good, "event": event})

    def test_generator_reads_committed_objects_and_all_nested_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def git(*args):
                return (
                    subprocess.check_output(["git", "-C", temp, *args], stderr=subprocess.DEVNULL)
                    .decode()
                    .strip()
                )

            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            text = ROOT + 'dependencies = [{ name = "safe" }]\n' + package("safe", "2")
            for path in (root / "uv.lock", root / "nested directory/uv.lock"):
                path.parent.mkdir(exist_ok=True)
                path.write_text(text)
            git("add", ".")
            git("commit", "-m", "Fixture")
            sha = git("rev-parse", "HEAD")
            (root / "uv.lock").write_text(text.replace('version = "2"', 'version = "1"'))
            (root / "untracked").mkdir()
            (root / "untracked/uv.lock").write_text("bad")
            first = snapshot.generate(
                root, "org/repo", sha, "refs/heads/main", "1:1", "https://example.invalid/1"
            )
            second = snapshot.generate(
                root, "org/repo", sha, "refs/heads/main", "2:1", "https://example.invalid/2"
            )
            self.assertEqual(set(first["manifests"]), {"uv.lock", "nested directory/uv.lock"})
            self.assertIn("pkg:pypi/safe@2", first["manifests"]["uv.lock"]["resolved"])
            self.assertEqual(first["job"]["correlator"], second["job"]["correlator"])
            self.assertNotEqual(first["job"]["id"], second["job"]["id"])
            json.dumps(first)  # Actual payload remains serializable without third-party packages.


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        def git(*args):
            return (
                subprocess.check_output(
                    ["git", "-C", self.temp.name, *args], stderr=subprocess.DEVNULL
                )
                .decode()
                .strip()
            )

        git("init")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (self.root / "uv.lock").write_text(
            ROOT + 'dependencies = [{ name = "safe" }]\n' + package("safe")
        )
        git("add", ".")
        git("commit", "-m", "Fixture")
        self.sha = git("rev-parse", "HEAD")
        self.env = {
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_SHA": self.sha,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_TOKEN": "test-token",
        }
        self.payload = snapshot.generate(
            self.root,
            "org/repo",
            self.sha,
            "refs/heads/main",
            "42:2",
            "https://github.com/org/repo/actions/runs/42",
        )
        self.calls = []

    def getter(self, repository, suffix="", token=None):
        self.calls.append(suffix or "metadata")
        self.assertEqual(token, "test-token")
        return {"sha": self.sha} if suffix else {"full_name": "org/repo", "default_branch": "main"}

    def poster(self, repository, payload, token):
        self.calls.append("POST")
        self.assertEqual(repository, "org/repo")
        self.assertEqual(payload, self.payload)
        return {"id": 123, "result": "SUCCESS"}

    def test_publish_guard_order_and_receipt(self):
        receipt = self.root / "receipt.json"
        result = snapshot.publish(
            self.payload,
            self.root,
            self.env,
            receipt_path=receipt,
            getter=self.getter,
            poster=self.poster,
        )
        self.assertEqual(result["id"], 123)
        self.assertEqual(
            self.calls, ["metadata", "/commits/main", "POST", "metadata", "/commits/main"]
        )
        saved = json.loads(receipt.read_text())
        self.assertEqual(saved["sha"], self.sha)
        self.assertIn("uv.lock", saved["manifest_hashes"])
        self.assertNotIn("test-token", receipt.read_text())

    def test_modified_payload_or_wrong_run_cannot_publish(self):
        for field, value in (
            ("sha", "b" * 40),
            ("ref", "refs/heads/feature"),
            ("metadata", {"repository": "attacker/fork"}),
            ("manifests", {}),
            ("job", {"id": "another-run", "correlator": "old-stream"}),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                snapshot.publish(
                    {**self.payload, field: value},
                    self.root,
                    self.env,
                    getter=self.getter,
                    poster=self.poster,
                )
        self.assertEqual(self.calls, [])

    def test_default_branch_rename_during_generation_prevents_post(self):
        def renamed(repository, suffix="", token=None):
            return (
                {"sha": self.sha}
                if suffix
                else {"full_name": "org/repo", "default_branch": "renamed"}
            )

        post = Mock()
        with self.assertRaisesRegex(ValueError, "live default branch"):
            snapshot.publish(self.payload, self.root, self.env, getter=renamed, poster=post)
        post.assert_not_called()

    def test_new_head_or_untrusted_event_prevents_post(self):
        for update in (
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "pull_request_target"},
            {"GITHUB_SHA": "b" * 40},
            {"GITHUB_TOKEN": ""},
        ):
            post = Mock()
            with self.subTest(update=update), self.assertRaises(ValueError):
                snapshot.publish(
                    self.payload, self.root, {**self.env, **update}, getter=self.getter, poster=post
                )
            post.assert_not_called()

    def test_push_during_post_is_visible_and_receipt_survives(self):
        def moved(repository, suffix="", token=None):
            if suffix and "POST" in self.calls:
                return {"sha": "b" * 40}
            return self.getter(repository, suffix, token)

        receipt = self.root / "receipt.json"
        with self.assertRaisesRegex(ValueError, "live default HEAD"):
            snapshot.publish(
                self.payload,
                self.root,
                self.env,
                receipt_path=receipt,
                getter=moved,
                poster=self.poster,
            )
        self.assertEqual(self.calls.count("POST"), 1)
        self.assertEqual(json.loads(receipt.read_text())["response"]["id"], 123)

    def test_deleted_last_lock_generates_empty_replacement_stream(self):
        subprocess.run(
            ["git", "-C", str(self.root), "rm", "uv.lock"], check=True, stdout=subprocess.DEVNULL
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Remove lock"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        sha = snapshot.git(self.root, "rev-parse", "HEAD").decode().strip()
        payload = snapshot.generate(
            self.root, "org/repo", sha, "refs/heads/main", "43:1", "https://example.invalid"
        )
        self.assertEqual(payload["manifests"], {})
        self.assertEqual(payload["job"]["correlator"], self.payload["job"]["correlator"])

    def test_http_redirects_fail_before_forwarding_token(self):
        with patch.object(snapshot, "build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 201
            response.read.return_value = b'{"id": 123}'
            snapshot.api_post_snapshot("org/repo", self.payload, "test-token")
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(
                request.full_url, "https://api.github.com/repos/org/repo/dependency-graph/snapshots"
            )
            self.assertEqual(request.get_method(), "POST")
            redirect_handler = opener.call_args.args[0]
            with self.assertRaisesRegex(ValueError, "redirects"):
                redirect_handler.redirect_request(
                    request, None, 302, "Found", {}, "https://attacker.invalid"
                )


if __name__ == "__main__":
    unittest.main()
