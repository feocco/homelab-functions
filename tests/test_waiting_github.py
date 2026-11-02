import base64
import json
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from homelab.github_waiting import INBOX_QUERY, GitHubWaitingSource, load_app_private_key
from homelab.json_http import JsonResponse
from homelab.waiting import Category, WaitingSourceError
from waiting_support import FakeTransport, raise_transport


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
NOW = 1_800_000_000.0
READ_ONLY = {"metadata": "read", "pull_requests": "read", "issues": "read", "checks": "read"}
REPO = "feocco/homelab-functions"


def pr_node(
    number,
    *,
    repo=REPO,
    state="OPEN",
    checks="SUCCESS",
    review=None,
    draft=False,
    mergeable="MERGEABLE",
    body="",
    comments=(),
    cross=False,
    **extra,
):
    url = f"https://github.com/{repo}/pull/{number}"
    node = {
        "__typename": "PullRequest",
        "number": number,
        "title": f"Fake PR {number}",
        "url": url,
        "body": body,
        "updatedAt": f"2026-11-01T1{number % 10}:00:00Z",
        "state": state,
        "isDraft": draft,
        "headRefName": f"cursor/fake-{number}",
        "isCrossRepository": cross,
        "reviewDecision": review,
        "mergeable": mergeable,
        "repository": {"nameWithOwner": repo},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]},
        "comments": {
            "nodes": [
                {"body": text, "url": f"{url}#issuecomment-{index}", "updatedAt": "2026-11-01T18:00:00Z"}
                for index, text in enumerate(comments)
            ]
        },
    }
    node.update(extra)
    return node


def issue_node(number, *, repo=REPO, body=""):
    return {
        "__typename": "Issue",
        "number": number,
        "title": f"Fake issue {number}",
        "url": f"https://github.com/{repo}/issues/{number}",
        "body": body,
        "updatedAt": "2026-11-01T09:00:00Z",
        "state": "OPEN",
        "repository": {"nameWithOwner": repo},
        "comments": {"nodes": []},
    }


def github(transport, *, permissions=READ_ONLY, open_nodes=(), merged_nodes=(), token_status=201, expires_at="2027-01-15T08:00:00Z"):
    transport.on(
        lambda r: r.url.endswith("/access_tokens"),
        lambda r: JsonResponse(
            token_status,
            {"token": "ghs_fake_installation_token", "expires_at": expires_at, "permissions": permissions}
            if token_status == 201
            else {"message": "Bad credentials"},
        ),
    )
    transport.on(
        lambda r: r.url.endswith("/graphql"),
        lambda r: JsonResponse(
            200,
            {"data": {"open": {"nodes": list(open_nodes)}, "merged": {"nodes": list(merged_nodes)}}},
        ),
    )


def source(transport, *, key=PEM, clock=lambda: NOW):
    return GitHubWaitingSource(
        app_id="5572135",
        installation_id="177648549",
        owner="feocco",
        private_key=key,
        transport=transport,
        clock=clock,
    )


def decode_segment(segment):
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def ask(kind, need, why, tail="", mark=" "):
    return f"- [{mark}] **Joe: {kind}** {need} Why: {why}{tail}"


class GitHubAppAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_signs_an_app_jwt_and_queries_open_and_recently_merged_sources(self):
        transport = FakeTransport()
        github(transport, open_nodes=[pr_node(14)])
        inbox = await source(transport).fetch()

        self.assertEqual([item.id for item in inbox.items], [f"{REPO}#14:approve_merge"])
        token_request, graphql_request = transport.requests
        self.assertEqual(token_request.method, "POST")
        self.assertEqual(token_request.url, "https://api.github.com/app/installations/177648549/access_tokens")
        jwt = token_request.headers["Authorization"].removeprefix("Bearer ")
        header, claims, signature = jwt.split(".")
        self.assertEqual(decode_segment(header), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(decode_segment(claims), {"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": "5572135"})
        KEY.public_key().verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            f"{header}.{claims}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        self.assertEqual(graphql_request.headers["Authorization"], "Bearer ghs_fake_installation_token")
        self.assertEqual(graphql_request.payload["query"], INBOX_QUERY)
        merged_since = datetime.fromtimestamp(NOW - 14 * 86400, timezone.utc).date().isoformat()
        self.assertEqual(
            graphql_request.payload["variables"],
            {
                "open": "is:open archived:false user:feocco sort:updated-desc",
                "merged": f"is:pr is:merged archived:false user:feocco merged:>={merged_since} sort:updated-desc",
            },
        )

    async def test_reuses_the_token_until_five_minutes_before_expiry(self):
        transport = FakeTransport()
        expires = NOW + 3600
        github(transport, expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat().replace("+00:00", "Z"))
        now = [NOW]
        github_source = source(transport, clock=lambda: now[0])

        def token_calls():
            return sum(1 for request in transport.requests if request.url.endswith("/access_tokens"))

        await github_source.fetch()
        now[0] = expires - 301
        await github_source.fetch()
        self.assertEqual(token_calls(), 1)
        now[0] = expires - 299
        await github_source.fetch()
        self.assertEqual(token_calls(), 2)

    async def test_refuses_a_token_with_write_permissions_before_any_query(self):
        transport = FakeTransport()
        github(transport, permissions={"metadata": "read", "pull_requests": "write", "contents": "write"})
        with self.assertRaises(WaitingSourceError) as caught:
            await source(transport).fetch()
        self.assertIn("read-only GitHub App", str(caught.exception))
        self.assertIn("contents: write", str(caught.exception))
        self.assertFalse(any(r.url.endswith("/graphql") for r in transport.requests))

    async def test_bad_credentials_name_the_settings_and_never_the_key(self):
        transport = FakeTransport()
        github(transport, token_status=401)
        with self.assertRaises(WaitingSourceError) as caught:
            await source(transport).fetch()
        message = str(caught.exception)
        self.assertIn("DASHBOARD_GH_APP_ID", message)
        self.assertIn("DASHBOARD_GH_APP_KEY", message)
        self.assertNotIn("PRIVATE KEY", message)
        self.assertNotIn(PEM.splitlines()[1], message)

    async def test_unreachable_github_is_a_source_error(self):
        transport = FakeTransport()
        transport.on(lambda r: True, raise_transport(maybe_delivered=False))
        with self.assertRaises(WaitingSourceError) as caught:
            await source(transport).fetch()
        self.assertIn("Could not reach GitHub", str(caught.exception))

    async def test_graphql_mutations_are_refused(self):
        transport = FakeTransport()
        github(transport)
        with self.assertRaises(ValueError):
            await source(transport).graphql("  mutation { mergePullRequest(input: {}) { clientMutationId } }", {})
        self.assertEqual(transport.requests, [])

    async def test_graphql_errors_surface_as_source_errors(self):
        transport = FakeTransport()
        github(transport)
        transport.handlers[1] = (
            transport.handlers[1][0],
            lambda r: JsonResponse(200, {"errors": [{"message": "Something went wrong"}]}),
        )
        with self.assertRaises(WaitingSourceError) as caught:
            await source(transport).fetch()
        self.assertIn("Something went wrong", str(caught.exception))


class PrivateKeyFormatTests(unittest.TestCase):
    def test_accepts_pem_escaped_pem_and_base64_pem(self):
        escaped = PEM.strip().replace("\n", "\\n")
        encoded = base64.b64encode(PEM.encode()).decode()
        for raw in (PEM, escaped, encoded):
            key = load_app_private_key(raw)
            self.assertEqual(key.private_numbers(), KEY.private_numbers())

    def test_first_line_only_gets_a_specific_fix(self):
        with self.assertRaises(WaitingSourceError) as caught:
            load_app_private_key("-----BEGIN RSA PRIVATE KEY-----")
        self.assertIn("only the first line", str(caught.exception))
        self.assertIn("base64", str(caught.exception))

    def test_garbage_is_rejected_without_echoing_it(self):
        with self.assertRaises(WaitingSourceError) as caught:
            load_app_private_key("not-a-key-value")
        self.assertNotIn("not-a-key-value", str(caught.exception))


class InboxTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, open_nodes=(), merged_nodes=()):
        transport = FakeTransport()
        github(transport, open_nodes=open_nodes, merged_nodes=merged_nodes)
        return await source(transport).fetch()

    async def test_only_pull_requests_waiting_on_joe_become_approve_merge_items(self):
        inbox = await self.fetch(
            [
                pr_node(1, checks="SUCCESS"),
                pr_node(2, checks=None, review="APPROVED"),
                pr_node(3, checks="FAILURE"),
                pr_node(4, checks="PENDING"),
                pr_node(5, draft=True),
                pr_node(6, review="CHANGES_REQUESTED"),
                pr_node(7, mergeable="CONFLICTING"),
            ]
        )
        by_number = {item.source_label: item for item in inbox.items}
        self.assertEqual(sorted(by_number), [f"{REPO}#1", f"{REPO}#2"])
        for item in inbox.items:
            self.assertIs(item.category, Category.APPROVE_MERGE)
            self.assertEqual(item.need, f"Approve and merge “Fake PR {item.source_label[-1]}”")
            self.assertEqual(item.action.kind, "open")
            self.assertEqual(item.action.url, item.source_url)
            self.assertEqual(item.action.label, "Review and merge on GitHub")
        self.assertEqual(by_number[f"{REPO}#1"].why, "Its checks passed and nothing else blocks it.")
        self.assertEqual(by_number[f"{REPO}#2"].why, "It is approved and its checks passed. It only needs merging.")

    async def test_ask_lines_become_items_in_their_category(self):
        body = "\n".join(
            (
                "Some context the agent wrote.",
                ask("deploy", "Roll out the image.", "macmini runs an old build.", " Action: https://github.com/feocco/homelab-config/actions"),
                ask("secret", "Add `FAKE_KEY`.", "the service cannot start."),
                ask("key", "Rotate the fake token.", "it leaked in a test log."),
                ask("dns", "Pick a hostname.", "two services want the same one."),
                ask("infra", "Approve a bigger disk.", "the volume is 92% full."),
                ask("question", "Skip Fridays?", "the routine needs one rule."),
                ask("copy", "Run the apply.", "it publishes the record.", " Copy: `./scripts/apply --watch`"),
                ask("evidence", "Check the screenshots.", "they prove the layout.", " Action: https://github.com/feocco/x/pull/1#evidence"),
            )
        )
        inbox = await self.fetch([pr_node(9, checks="FAILURE", body=body)])
        found = {(item.category, item.need): item for item in inbox.items}
        self.assertEqual(
            sorted((category.value, need) for category, need in found),
            sorted(
                [
                    ("deploy", "Roll out the image."),
                    ("secret", "Add `FAKE_KEY`."),
                    ("secret", "Rotate the fake token."),
                    ("infra", "Pick a hostname."),
                    ("infra", "Approve a bigger disk."),
                    ("question", "Skip Fridays?"),
                    ("copy", "Run the apply."),
                    ("evidence", "Check the screenshots."),
                ]
            ),
        )
        copy = found[(Category.COPY, "Run the apply.")]
        self.assertEqual((copy.action.kind, copy.action.label, copy.action.value), ("copy", "Copy", "./scripts/apply --watch"))
        deploy = found[(Category.DEPLOY, "Roll out the image.")]
        self.assertEqual((deploy.action.label, deploy.action.url), ("Review the deploy", "https://github.com/feocco/homelab-config/actions"))
        question = found[(Category.QUESTION, "Skip Fridays?")]
        self.assertEqual((question.action.label, question.action.url), ("Answer on GitHub", question.source_url))
        self.assertEqual(question.why, "The routine needs one rule.")
        self.assertTrue(all(item.source_url == f"https://github.com/{REPO}/pull/9" for item in inbox.items))
        self.assertTrue(all(item.head_ref == "cursor/fake-9" for item in inbox.items))
        self.assertEqual(inbox.rejected, [])

    async def test_checked_or_removed_asks_are_handled(self):
        open_ask = ask("question", "Skip Fridays?", "the routine needs one rule.")
        before = await self.fetch([issue_node(3, body=open_ask)])
        after_tick = await self.fetch([issue_node(3, body=open_ask.replace("[ ]", "[x]"))])
        after_delete = await self.fetch([issue_node(3, body="Thanks, decided.")])
        self.assertEqual([item.category for item in before.items], [Category.QUESTION])
        self.assertEqual(after_tick.items, [])
        self.assertEqual(after_delete.items, [])

    async def test_item_ids_stay_stable_across_edits_elsewhere(self):
        line = ask("secret", "Add `FAKE_KEY`.", "the service cannot start.")
        first = await self.fetch([pr_node(9, checks="FAILURE", body=line)])
        second = await self.fetch([pr_node(9, checks="FAILURE", body=f"New intro paragraph.\n\n{line}")])
        self.assertEqual([item.id for item in first.items], [item.id for item in second.items])
        self.assertRegex(first.items[0].id, rf"^{REPO}#9:secret:[0-9a-f]{{10}}$")

    async def test_comments_issues_and_recently_merged_prs_are_sources(self):
        comment = ask("evidence", "Check the trace.", "it shows the race.")
        deploy = ask("deploy", "Roll out the merged image.", "the merge does not deploy it.")
        inbox = await self.fetch(
            open_nodes=[pr_node(4, checks="PENDING", comments=(comment,)), issue_node(5, body=ask("question", "Which disk?", "pick one."))],
            merged_nodes=[pr_node(6, state="MERGED", body=deploy)],
        )
        sources = {item.category: (item.source_label, item.source_url, item.head_ref) for item in inbox.items}
        self.assertEqual(
            sources[Category.EVIDENCE],
            (f"{REPO}#4 comment", f"https://github.com/{REPO}/pull/4#issuecomment-0", "cursor/fake-4"),
        )
        self.assertEqual(sources[Category.QUESTION], (f"{REPO}#5", f"https://github.com/{REPO}/issues/5", None))
        self.assertEqual(sources[Category.DEPLOY], (f"{REPO}#6", f"https://github.com/{REPO}/pull/6", None))
        self.assertNotIn(Category.APPROVE_MERGE, sources)

    async def test_malformed_asks_are_reported_not_shown(self):
        body = "\n".join(
            (
                ask("approve", "Merge this.", "agents cannot ask for merges."),
                "- [ ] **Joe: secret** Add a key with no reason.",
                ask("copy", "Run something.", "no value given."),
                ask("deploy", "Deploy.", "now.", " Action: javascript:alert(1)"),
                ask("secret", "Add a key.", "because.", " Copy: `SECRET_NAME`"),
            )
        )
        inbox = await self.fetch([pr_node(9, checks="FAILURE", body=body)])
        self.assertEqual(inbox.items, [])
        self.assertEqual(len(inbox.rejected), 5)
        self.assertIn("Unknown kind 'approve'", inbox.rejected[0].reason)
        self.assertTrue(all(rejected.source_url == f"https://github.com/{REPO}/pull/9" for rejected in inbox.rejected))

    async def test_foreign_owners_mismatched_urls_closed_and_unknown_nodes_yield_nothing(self):
        line = ask("question", "Hi?", "test.")
        inbox = await self.fetch(
            [
                pr_node(1, repo="someone-else/repo", body=line),
                pr_node(2, url="https://evil.example/feocco/homelab-functions/pull/2", body=line),
                pr_node(3, state="CLOSED", body=line),
                {"__typename": "Discussion", "body": line},
                {},
            ]
        )
        self.assertEqual((inbox.items, inbox.rejected), ([], []))

    async def test_fork_prs_point_feedback_at_the_pr_not_a_branch(self):
        inbox = await self.fetch([pr_node(8, cross=True)])
        (item,) = inbox.items
        self.assertEqual((item.head_ref, item.pr_url), (None, f"https://github.com/{REPO}/pull/8"))


if __name__ == "__main__":
    unittest.main()
