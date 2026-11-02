"""Cursor Cloud Agents API client for Waiting on me feedback.

Only three calls are made: launch an agent, send it a follow-up, and read the
pull request it opened. The launch body is built from a fixed field list, so
this client cannot ask Cursor to merge, approve, or deploy.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from .json_http import JsonResponse, JsonTransport, TransportError
from .waiting import CursorCallError, WaitingItem


CURSOR_API_URL = "https://api.cursor.com"
LAUNCH_FIELDS = frozenset({"agentId", "name", "prompt", "repos", "autoCreatePR", "workOnCurrentBranch"})
_AGENT_URL_PREFIX = "https://cursor.com/"
_PR_URL_PREFIX = "https://github.com/"


def launch_body(*, agent_id: str, name: str, prompt: str, item: WaitingItem) -> dict[str, Any]:
    """Return the `POST /v1/agents` body for one item's feedback agent."""

    repo: dict[str, str] = {"url": f"https://github.com/{item.repo}"}
    if item.cross_repository:
        repo["prUrl"] = item.url
    else:
        repo["startingRef"] = item.head_ref
    body = {
        "agentId": agent_id,
        "name": name,
        "prompt": {"text": prompt},
        "repos": [repo],
        "autoCreatePR": True,
        "workOnCurrentBranch": False,
    }
    if set(body) != LAUNCH_FIELDS:
        raise AssertionError("launch body fields drifted from LAUNCH_FIELDS")
    return body


class CursorAgentsClient:
    """Launch and continue Cursor Cloud Agents with `CURSOR_API_KEY`.

    ```python
    client = CursorAgentsClient(api_key, AiohttpJsonTransport(10))
    url = await client.launch(agent_id="bc-…", name="…", prompt="…", item=item)
    delivery = await client.follow_up("bc-…", "More feedback")
    ```
    """

    def __init__(self, api_key: str, transport: JsonTransport, base_url: str = CURSOR_API_URL) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        credentials = base64.b64encode(f"{api_key}:".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "User-Agent": "homelab-functions",
        }
        self._transport = transport
        self._base_url = base_url.rstrip("/")

    async def launch(self, *, agent_id: str, name: str, prompt: str, item: WaitingItem) -> str:
        body = launch_body(agent_id=agent_id, name=name, prompt=prompt, item=item)
        response = await self._call("POST", "/v1/agents", body)
        agent = response.body.get("agent") if isinstance(response.body, dict) else None
        url = agent.get("url") if isinstance(agent, dict) else None
        if isinstance(url, str) and url.startswith(_AGENT_URL_PREFIX):
            return url
        return f"https://cursor.com/agents/{agent_id}"

    async def follow_up(self, agent_id: str, prompt: str) -> str:
        """Start a follow-up run. Steer the running turn instead when the agent is busy."""

        path = f"/v1/agents/{quote(agent_id, safe='')}/runs"
        try:
            await self._call("POST", path, {"prompt": {"text": prompt}})
            return "followup"
        except CursorCallError as exc:
            if exc.code != "agent_busy":
                raise
            busy = exc
        try:
            await self._call("POST", path, {"prompt": {"text": prompt}, "delivery": "steer"})
        except CursorCallError as exc:
            if exc.maybe_delivered:
                raise
            raise busy from exc
        return "steer"

    async def pull_request_url(self, agent_id: str) -> str | None:
        agent_path = f"/v1/agents/{quote(agent_id, safe='')}"
        agent = (await self._call("GET", agent_path)).body
        run_id = agent.get("latestRunId") if isinstance(agent, dict) else None
        if not isinstance(run_id, str) or not run_id:
            return None
        run = (await self._call("GET", f"{agent_path}/runs/{quote(run_id, safe='')}")).body
        git = run.get("git") if isinstance(run, dict) else None
        branches = git.get("branches") if isinstance(git, dict) else None
        for branch in branches if isinstance(branches, list) else []:
            pr_url = branch.get("prUrl") if isinstance(branch, dict) else None
            if isinstance(pr_url, str) and pr_url.startswith(_PR_URL_PREFIX):
                return pr_url
        return None

    async def _call(self, method: str, path: str, payload: Any | None = None) -> JsonResponse:
        headers = dict(self._headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._transport.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                payload=payload,
            )
        except TransportError as exc:
            raise CursorCallError(
                f"Cursor API {exc}",
                code="cursor_unreachable",
                maybe_delivered=exc.maybe_delivered,
            ) from exc
        if 200 <= response.status < 300:
            return response
        code, message = _error_details(response)
        raise CursorCallError(
            message,
            code=code,
            status=response.status,
            maybe_delivered=response.status >= 500 or response.status == 408,
        )


def _error_details(response: JsonResponse) -> tuple[str, str]:
    body = response.body if isinstance(response.body, dict) else {}
    error = body.get("error")
    if isinstance(error, dict):
        code, message = error.get("code"), error.get("message")
    else:
        code, message = body.get("code") or error, body.get("message")
    code = code if isinstance(code, str) and code else f"http_{response.status}"
    message = message if isinstance(message, str) and message else f"HTTP {response.status}"
    return code, message[:300]
