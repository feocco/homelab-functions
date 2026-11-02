import base64
import json
import unittest
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from homelab.github_waiting import WAITING_QUERY, GitHubWaitingSource, load_app_private_key
from homelab.json_http import JsonResponse
from homelab.waiting import ItemState, WaitingSourceError
from waiting_support import FakeTransport, raise_transport


KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
NOW = 1_800_000_000.0
READ_ONLY = {"metadata": "read", "pull_requests": "read", "checks": "read", "statuses": "read"}


def pr_node(number, *, repo="feocco/homelab-functions", rollup="SUCCESS", contexts=(), review=None, **extra):
    node = {
        "number": number,
        "title": f"Fake PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "createdAt": "2026-11-01T10:00:00Z",
        "updatedAt": f"2026-11-01T1{number % 10}:00:00Z",
        "headRefName": f"cursor/fake-{number}",
        "isCrossRepository": False,
        "reviewDecision": review,
        "author": {"login": "cursor"},
        "repository": {"nameWithOwner": repo},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": None
                        if rollup is None
                        else {"state": rollup, "contexts": {"nodes": list(contexts)}}
                    }
                }
            ]
        },
    }
    node.update(extra)
    return node


def github(transport, *, permissions=READ_ONLY, nodes=(), token_status=201, expires_at="2027-01-15T08:00:00Z"):
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
        lambda r: JsonResponse(200, {"data": {"search": {"nodes": list(nodes)}}}),
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


class GitHubAppAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_signs_an_app_jwt_and_uses_the_installation_token(self):
        transport = FakeTransport()
        github(transport, nodes=[pr_node(14)])
        items = await source(transport).fetch()

        self.assertEqual([item.id for item in items], ["feocco/homelab-functions#14"])
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
        self.assertEqual(graphql_request.payload["query"], WAITING_QUERY)
        self.assertEqual(
            graphql_request.payload["variables"],
            {"search": "is:pr is:open draft:false archived:false user:feocco sort:updated-desc"},
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


class AggregationTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, nodes):
        transport = FakeTransport()
        github(transport, nodes=nodes)
        return await source(transport).fetch()

    async def test_classifies_check_rollups_and_review_state(self):
        items = await self.fetch(
            [
                pr_node(1, rollup="SUCCESS"),
                pr_node(
                    2,
                    rollup="FAILURE",
                    contexts=[
                        {"__typename": "CheckRun", "name": "pytest", "conclusion": "FAILURE", "detailsUrl": "https://github.com/x"},
                        {"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS", "detailsUrl": "https://github.com/y"},
                        {"__typename": "StatusContext", "context": "ci/legacy", "state": "ERROR", "targetUrl": "javascript:alert(1)"},
                    ],
                ),
                pr_node(3, rollup="PENDING"),
                pr_node(4, rollup="SUCCESS", review="CHANGES_REQUESTED"),
                pr_node(5, rollup=None),
            ]
        )
        by_number = {item.number: item for item in items}
        self.assertEqual(by_number[1].state, ItemState.READY)
        self.assertEqual(by_number[2].state, ItemState.FAILING)
        self.assertEqual(
            [(check.name, check.url) for check in by_number[2].failing_checks],
            [("pytest", "https://github.com/x"), ("ci/legacy", None)],
        )
        self.assertEqual(by_number[3].state, ItemState.RUNNING)
        self.assertEqual(by_number[4].state, ItemState.CHANGES_REQUESTED)
        self.assertEqual((by_number[5].state, by_number[5].checks), (ItemState.READY, "none"))

    async def test_drops_foreign_owners_mismatched_urls_and_non_pr_nodes(self):
        items = await self.fetch(
            [
                pr_node(1),
                pr_node(2, repo="someone-else/repo"),
                pr_node(3, url="https://evil.example/feocco/homelab-functions/pull/3"),
                {},
                {"number": "4"},
            ]
        )
        self.assertEqual([item.id for item in items], ["feocco/homelab-functions#1"])

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


if __name__ == "__main__":
    unittest.main()
