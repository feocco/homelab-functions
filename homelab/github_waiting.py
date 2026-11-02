"""Read open pull requests for the Waiting on me dashboard with a GitHub App.

The App authenticates with a short-lived RS256 JWT and exchanges it for an
installation token. A token with any permission other than `read` is refused,
and only GraphQL queries are sent, so this module cannot change GitHub.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .json_http import JsonTransport, TransportError
from .waiting import FailingCheck, ItemState, WaitingItem, WaitingSourceError


GITHUB_API_URL = "https://api.github.com"
SEARCH_LIMIT = 50
_TOKEN_REFRESH_MARGIN_SECONDS = 300
_FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_FAILED_STATUSES = {"FAILURE", "ERROR"}
_MUTATION = re.compile(r"^\s*mutation\b", re.IGNORECASE)

WAITING_QUERY = """
query WaitingOnMe($search: String!) {
  search(query: $search, type: ISSUE, first: %d) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        createdAt
        updatedAt
        headRefName
        isCrossRepository
        reviewDecision
        author { login }
        repository { nameWithOwner }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 50) {
                  nodes {
                    __typename
                    ... on CheckRun { name conclusion detailsUrl }
                    ... on StatusContext { context state targetUrl }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
""" % SEARCH_LIMIT


def load_app_private_key(raw: str) -> rsa.RSAPrivateKey:
    """Parse `DASHBOARD_GH_APP_KEY` as PEM, PEM with `\\n` escapes, or base64 PEM.

    `.env` files hold one line per value, so a raw multi-line PEM arrives
    truncated to its BEGIN line. That case gets its own error.
    """

    text = raw.strip()
    if "-----BEGIN" not in text:
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise WaitingSourceError(
                "DASHBOARD_GH_APP_KEY is not a PEM key or base64-encoded PEM."
            ) from exc
    text = text.replace("\\n", "\n")
    if "-----END" not in text:
        raise WaitingSourceError(
            "DASHBOARD_GH_APP_KEY holds only the first line of the PEM key. "
            "Store it base64-encoded (base64 -w0 key.pem) or with \\n escapes."
        )
    try:
        key = serialization.load_pem_private_key(text.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise WaitingSourceError("DASHBOARD_GH_APP_KEY is not a readable PEM private key.") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise WaitingSourceError("DASHBOARD_GH_APP_KEY must be the App's RSA private key.")
    return key


def app_jwt(app_id: str, key: rsa.RSAPrivateKey, now: float) -> str:
    """Return a GitHub App JWT valid for nine minutes, backdated for clock drift."""

    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": int(now) - 60, "exp": int(now) + 540, "iss": app_id}
    signing_input = f"{_b64url_json(header)}.{_b64url_json(claims)}"
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _b64url_json(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GitHubWaitingSource:
    """Fetch open, non-draft pull requests for one owner through a read-only App.

    ```python
    source = GitHubWaitingSource(
        app_id="123", installation_id="456", owner="feocco",
        private_key=os.environ["DASHBOARD_GH_APP_KEY"],
        transport=AiohttpJsonTransport(10),
    )
    items = await source.fetch()
    ```
    """

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: str,
        owner: str,
        private_key: str,
        transport: JsonTransport,
        clock: Callable[[], float] = time.time,
        api_url: str = GITHUB_API_URL,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.owner = owner
        self._raw_key = private_key
        self._key: rsa.RSAPrivateKey | None = None
        self._transport = transport
        self._clock = clock
        self._api_url = api_url.rstrip("/")
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def fetch(self) -> list[WaitingItem]:
        search = f"is:pr is:open draft:false archived:false user:{self.owner} sort:updated-desc"
        data = await self.graphql(WAITING_QUERY, {"search": search})
        nodes = data.get("search", {}).get("nodes", []) if isinstance(data, dict) else []
        items = (waiting_item_from_node(node, self.owner) for node in nodes if isinstance(node, dict))
        return [item for item in items if item is not None]

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if _MUTATION.match(query):
            raise ValueError("The waiting dashboard only sends GraphQL queries")
        token = await self._installation_token()
        response = await self._request(
            "POST",
            "/graphql",
            {"Authorization": f"Bearer {token}"},
            {"query": query, "variables": variables},
        )
        if response.status == 401:
            self._token = None
        body = response.body if isinstance(response.body, dict) else {}
        if response.status != 200:
            raise WaitingSourceError(f"GitHub GraphQL returned HTTP {response.status}.")
        errors = body.get("errors")
        if errors:
            first = errors[0].get("message") if isinstance(errors[0], dict) else None
            raise WaitingSourceError(f"GitHub rejected the query: {first or 'unknown error'}")
        return body.get("data") or {}

    async def _installation_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        if self._key is None:
            self._key = load_app_private_key(self._raw_key)
        jwt = app_jwt(self.app_id, self._key, now)
        response = await self._request(
            "POST",
            f"/app/installations/{self.installation_id}/access_tokens",
            {"Authorization": f"Bearer {jwt}"},
            None,
        )
        body = response.body if isinstance(response.body, dict) else {}
        if response.status in (401, 403):
            raise WaitingSourceError(
                f"GitHub rejected the App credentials (HTTP {response.status}). "
                "Check DASHBOARD_GH_APP_ID and DASHBOARD_GH_APP_KEY."
            )
        if response.status == 404:
            raise WaitingSourceError(
                "GitHub has no installation with DASHBOARD_GH_APP_INSTALLATION_ID for this App."
            )
        token = body.get("token")
        if response.status != 201 or not isinstance(token, str):
            raise WaitingSourceError(f"GitHub returned HTTP {response.status} for the installation token.")
        permissions = body.get("permissions")
        writable = sorted(
            f"{name}: {level}"
            for name, level in (permissions.items() if isinstance(permissions, dict) else ())
            if level != "read"
        )
        if writable:
            raise WaitingSourceError(
                "The dashboard only runs with a read-only GitHub App, and this App can write "
                f"({', '.join(writable)})."
            )
        self._token = token
        self._token_expires_at = _parse_expiry(body.get("expires_at"), fallback=now + 3600)
        return token

    async def _request(
        self,
        method: str,
        path: str,
        auth: dict[str, str],
        payload: Any | None,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homelab-functions-waiting-dashboard",
            **auth,
        }
        try:
            return await self._transport.request(
                method,
                f"{self._api_url}{path}",
                headers=headers,
                payload=payload,
            )
        except TransportError as exc:
            raise WaitingSourceError(f"Could not reach GitHub: {exc}.") from exc


def _parse_expiry(value: Any, *, fallback: float) -> float:
    if not isinstance(value, str):
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def waiting_item_from_node(node: dict[str, Any], owner: str) -> WaitingItem | None:
    """Parse one GraphQL pull request node. Returns None for anything malformed."""

    repo = (node.get("repository") or {}).get("nameWithOwner")
    number = node.get("number")
    url = node.get("url")
    title = node.get("title")
    if not (isinstance(repo, str) and isinstance(number, int) and isinstance(url, str) and isinstance(title, str)):
        return None
    if repo.split("/", 1)[0].lower() != owner.lower():
        return None
    if url != f"https://github.com/{repo}/pull/{number}":
        return None
    rollup = _rollup(node)
    checks = _checks_state(rollup.get("state"))
    failing = _failing_checks(rollup)
    if checks == "failing":
        state = ItemState.FAILING
    elif checks == "running":
        state = ItemState.RUNNING
    elif node.get("reviewDecision") == "CHANGES_REQUESTED":
        state = ItemState.CHANGES_REQUESTED
    else:
        state = ItemState.READY
    author = (node.get("author") or {}).get("login")
    return WaitingItem(
        repo=repo,
        number=number,
        title=title,
        url=url,
        author=author if isinstance(author, str) else "ghost",
        state=state,
        checks=checks,
        failing_checks=failing,
        created_at=str(node.get("createdAt") or ""),
        updated_at=str(node.get("updatedAt") or ""),
        head_ref=str(node.get("headRefName") or ""),
        cross_repository=bool(node.get("isCrossRepository")),
    )


def _rollup(node: dict[str, Any]) -> dict[str, Any]:
    commits = (node.get("commits") or {}).get("nodes") or []
    commit = (commits[-1] or {}).get("commit") if commits else None
    rollup = (commit or {}).get("statusCheckRollup")
    return rollup if isinstance(rollup, dict) else {}


def _checks_state(rollup_state: Any) -> str:
    if rollup_state == "SUCCESS":
        return "passed"
    if rollup_state in ("FAILURE", "ERROR"):
        return "failing"
    if rollup_state in ("PENDING", "EXPECTED"):
        return "running"
    return "none"


def _failing_checks(rollup: dict[str, Any]) -> tuple[FailingCheck, ...]:
    contexts = (rollup.get("contexts") or {}).get("nodes") or []
    failing: list[FailingCheck] = []
    for context in contexts:
        if not isinstance(context, dict):
            continue
        if context.get("__typename") == "CheckRun" and context.get("conclusion") in _FAILED_CONCLUSIONS:
            name, url = context.get("name"), context.get("detailsUrl")
        elif context.get("__typename") == "StatusContext" and context.get("state") in _FAILED_STATUSES:
            name, url = context.get("context"), context.get("targetUrl")
        else:
            continue
        if isinstance(name, str) and name:
            safe_url = url if isinstance(url, str) and url.startswith("https://") else None
            failing.append(FailingCheck(name, safe_url))
    return tuple(failing[:5])
