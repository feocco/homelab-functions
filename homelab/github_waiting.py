"""Build Joe's Waiting on me inbox from GitHub with a read-only GitHub App.

The App authenticates with a short-lived RS256 JWT and exchanges it for an
installation token. A token with any permission other than `read` is refused,
and only GraphQL queries are sent, so this module cannot change GitHub.

Two sources become inbox items:

- An open, non-draft pull request with passing (or no) checks, no requested
  changes, and no merge conflict becomes one "PR approve/merge" item.
- An unchecked ask line in a pull request or issue body or comment becomes
  one item in its category. Agents write ask lines like this:

      - [ ] **Joe: secret** Set `DASHBOARD_GH_APP_KEY`. Why: the inbox cannot read GitHub. Action: https://github.com/…
      - [ ] **Joe: copy** Publish the DNS record. Why: the route needs it. Copy: `./scripts/dispatch-route-dns --mode apply`

  Kinds: deploy, secret (or key), infra (or dns), question, copy, evidence.
  Ticking the box, deleting the line, or closing the source removes the item.
  Asks on pull requests merged in the last 14 days still count, because a
  deploy usually waits on a merged change.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .json_http import JsonTransport, TransportError
from .waiting import Category, Inbox, InboxItem, PrimaryAction, RejectedAsk, WaitingSourceError


GITHUB_API_URL = "https://api.github.com"
OPEN_LIMIT = 50
MERGED_LIMIT = 30
MERGED_WINDOW_DAYS = 14
_TOKEN_REFRESH_MARGIN_SECONDS = 300
_MUTATION = re.compile(r"^\s*mutation\b", re.IGNORECASE)

_COMMENTS = "comments(last: 30) { nodes { body url updatedAt } }"
_SOURCE_FIELDS = f"""
      __typename
      ... on PullRequest {{
        number title url body updatedAt state isDraft
        headRefName isCrossRepository reviewDecision mergeable
        repository {{ nameWithOwner }}
        commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
        {_COMMENTS}
      }}
      ... on Issue {{
        number title url body updatedAt state
        repository {{ nameWithOwner }}
        {_COMMENTS}
      }}"""
INBOX_QUERY = f"""
query WaitingOnMe($open: String!, $merged: String!) {{
  open: search(query: $open, type: ISSUE, first: {OPEN_LIMIT}) {{ nodes {{{_SOURCE_FIELDS}
  }} }}
  merged: search(query: $merged, type: ISSUE, first: {MERGED_LIMIT}) {{ nodes {{{_SOURCE_FIELDS}
  }} }}
}}
"""

ASK_KINDS: dict[str, Category] = {
    "deploy": Category.DEPLOY,
    "secret": Category.SECRET,
    "key": Category.SECRET,
    "infra": Category.INFRA,
    "dns": Category.INFRA,
    "question": Category.QUESTION,
    "copy": Category.COPY,
    "evidence": Category.EVIDENCE,
}
ACTION_LABELS: dict[Category, str] = {
    Category.APPROVE_MERGE: "Review and merge on GitHub",
    Category.DEPLOY: "Review the deploy",
    Category.SECRET: "Add the secret",
    Category.INFRA: "Make the decision",
    Category.QUESTION: "Answer on GitHub",
    Category.COPY: "Copy",
    Category.EVIDENCE: "Review the evidence",
}
_ASK_LINE = re.compile(r"^\s*[-*]\s+\[(?P<mark>[ xX])\]\s+\*\*Joe:\s*(?P<kind>[^*]+?)\s*\*\*\s*(?P<rest>.*)$")
_ASK_TAIL = re.compile(r"^(?P<why>.+?)(?:\s+(?P<target_kind>Action|Copy):\s+(?P<target>\S.*))?$")
_FENCE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<rest>.*)$")
_MAX_NEED, _MAX_WHY, _MAX_COPY = 300, 600, 2000


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
    """Build Joe's inbox for one owner from GitHub, through a read-only App.

    ```python
    source = GitHubWaitingSource(
        app_id="123", installation_id="456", owner="feocco",
        private_key=os.environ["DASHBOARD_GH_APP_KEY"],
        transport=AiohttpJsonTransport(10),
    )
    inbox = await source.fetch()
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

    async def fetch(self) -> Inbox:
        merged_since = datetime.fromtimestamp(self._clock(), timezone.utc) - timedelta(days=MERGED_WINDOW_DAYS)
        variables = {
            "open": f"is:open archived:false user:{self.owner} sort:updated-desc",
            "merged": (
                f"is:pr is:merged archived:false user:{self.owner} "
                f"merged:>={merged_since.date().isoformat()} sort:updated-desc"
            ),
        }
        data = await self.graphql(INBOX_QUERY, variables)
        items: list[InboxItem] = []
        rejected: list[RejectedAsk] = []
        for key in ("open", "merged"):
            search = data.get(key) if isinstance(data, dict) else None
            nodes = search.get("nodes", []) if isinstance(search, dict) else []
            for node in nodes:
                if isinstance(node, dict):
                    found, refused = inbox_from_node(node, self.owner)
                    items.extend(found)
                    rejected.extend(refused)
        return Inbox(items, rejected)

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if _MUTATION.match(query):
            raise ValueError("The waiting inbox only sends GraphQL queries")
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
                "The inbox only runs with a read-only GitHub App, and this App can write "
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
            "User-Agent": "homelab-functions-waiting-inbox",
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


def inbox_from_node(node: dict[str, Any], owner: str) -> tuple[list[InboxItem], list[RejectedAsk]]:
    """Turn one GraphQL pull request or issue node into inbox items.

    Anything malformed, from another owner, or not on github.com yields nothing.
    """

    kind = node.get("__typename")
    repo = (node.get("repository") or {}).get("nameWithOwner")
    number = node.get("number")
    url = node.get("url")
    if kind not in ("PullRequest", "Issue"):
        return [], []
    if not (isinstance(repo, str) and isinstance(number, int) and isinstance(url, str)):
        return [], []
    if repo.split("/", 1)[0].lower() != owner.lower():
        return [], []
    path = "pull" if kind == "PullRequest" else "issues"
    if url != f"https://github.com/{repo}/{path}/{number}":
        return [], []
    state = node.get("state")
    if state not in ("OPEN", "MERGED"):
        return [], []

    is_open_pr = kind == "PullRequest" and state == "OPEN"
    cross = bool(node.get("isCrossRepository"))
    head_ref = node.get("headRefName") if is_open_pr and not cross else None
    context = {
        "repo": repo,
        "number": number,
        "head_ref": head_ref if isinstance(head_ref, str) and head_ref else None,
        "pr_url": url if is_open_pr and cross else None,
    }
    label = f"{repo}#{number}"
    updated_at = str(node.get("updatedAt") or "")
    items: list[InboxItem] = []
    rejected: list[RejectedAsk] = []

    if is_open_pr:
        merge = approve_merge_item(node, label=label, updated_at=updated_at, **context)
        if merge is not None:
            items.append(merge)

    sources = [(node.get("body"), url, label, updated_at)]
    for comment in ((node.get("comments") or {}).get("nodes") or []):
        if isinstance(comment, dict) and str(comment.get("url") or "").startswith(f"{url}#"):
            sources.append((comment.get("body"), comment["url"], f"{label} comment", str(comment.get("updatedAt") or updated_at)))
    for text, source_url, source_label, source_updated in sources:
        if isinstance(text, str):
            found, refused = parse_asks(
                text,
                source_url=source_url,
                source_label=source_label,
                updated_at=source_updated,
                **context,
            )
            items.extend(found)
            rejected.extend(refused)
    return items, rejected


def approve_merge_item(
    node: dict[str, Any],
    *,
    repo: str,
    number: int,
    label: str,
    updated_at: str,
    head_ref: str | None,
    pr_url: str | None,
) -> InboxItem | None:
    """Return the approve/merge item when nothing but Joe blocks the pull request."""

    if node.get("isDraft") or node.get("reviewDecision") == "CHANGES_REQUESTED":
        return None
    if node.get("mergeable") == "CONFLICTING":
        return None
    commits = (node.get("commits") or {}).get("nodes") or []
    commit = (commits[-1] or {}).get("commit") if commits else None
    rollup = (commit or {}).get("statusCheckRollup")
    checks = rollup.get("state") if isinstance(rollup, dict) else None
    if checks not in ("SUCCESS", None):
        return None
    if node.get("reviewDecision") == "APPROVED":
        why = "It is approved and its checks passed. It only needs merging."
    elif checks == "SUCCESS":
        why = "Its checks passed and nothing else blocks it."
    else:
        why = "It has no checks and nothing else blocks it."
    title = str(node.get("title") or "").strip() or label
    url = node["url"]
    return InboxItem(
        id=f"{repo}#{number}:{Category.APPROVE_MERGE.value}",
        category=Category.APPROVE_MERGE,
        need=f"Approve and merge “{title}”",
        why=why,
        action=PrimaryAction("open", ACTION_LABELS[Category.APPROVE_MERGE], url=url),
        source_url=url,
        source_label=label,
        repo=repo,
        updated_at=updated_at,
        head_ref=head_ref,
        pr_url=pr_url,
    )


def parse_asks(
    text: str,
    *,
    repo: str,
    number: int,
    source_url: str,
    source_label: str,
    updated_at: str,
    head_ref: str | None,
    pr_url: str | None,
) -> tuple[list[InboxItem], list[RejectedAsk]]:
    """Read the unchecked `**Joe: kind**` task-list lines in one body or comment."""

    items: list[InboxItem] = []
    rejected: list[RejectedAsk] = []
    for line in _markdown_lines(text):
        match = _ASK_LINE.match(line)
        if match is None or match["mark"] != " ":
            continue

        def refuse(reason: str) -> None:
            rejected.append(RejectedAsk(source_url, line.strip()[:200], reason))

        category = ASK_KINDS.get(match["kind"].strip().lower())
        if category is None:
            refuse(f"Unknown kind {match['kind'].strip()!r}. Use one of: {', '.join(sorted(ASK_KINDS))}.")
            continue
        need, separator, tail = match["rest"].partition(" Why: ")
        need = need.strip()
        tail_match = _ASK_TAIL.match(tail.strip()) if separator else None
        if not need or tail_match is None:
            refuse("An ask needs what is needed, then ' Why: ' and the reason.")
            continue
        why = tail_match["why"].strip()
        why = why[:1].upper() + why[1:]
        target_kind, target = tail_match["target_kind"], (tail_match["target"] or "").strip()
        if len(need) > _MAX_NEED or len(why) > _MAX_WHY:
            refuse(f"Keep the need under {_MAX_NEED} characters and the reason under {_MAX_WHY}.")
            continue
        if category is Category.COPY:
            value = target.strip("`").strip() if target_kind == "Copy" else ""
            if not value or len(value) > _MAX_COPY:
                refuse("A copy ask ends with ' Copy: `the value`'.")
                continue
            action = PrimaryAction("copy", ACTION_LABELS[Category.COPY], value=value)
        elif target_kind == "Action":
            if not re.fullmatch(r"https://\S+", target):
                refuse("Action must be one https URL.")
                continue
            action = PrimaryAction("open", ACTION_LABELS[category], url=target)
        elif target_kind == "Copy":
            refuse("Only copy asks take a Copy value. Use Action: with a URL instead.")
            continue
        else:
            action = PrimaryAction("open", ACTION_LABELS[category], url=source_url)
        digest = hashlib.sha1(" ".join(need.lower().split()).encode()).hexdigest()[:10]
        items.append(
            InboxItem(
                id=f"{repo}#{number}:{category.value}:{digest}",
                category=category,
                need=need,
                why=why,
                action=action,
                source_url=source_url,
                source_label=source_label,
                repo=repo,
                updated_at=updated_at,
                head_ref=head_ref,
                pr_url=pr_url,
            )
        )
    return items, rejected


def _markdown_lines(text: str) -> Iterator[str]:
    """Yield prose lines, excluding fenced code blocks that only show examples."""

    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match is not None:
                fence = match["marker"]
                continue
            yield line
            continue
        if (
            match is not None
            and match["marker"][0] == fence[0]
            and len(match["marker"]) >= len(fence)
            and not match["rest"].strip()
        ):
            fence = None
