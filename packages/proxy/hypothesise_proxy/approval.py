"""Human-in-the-loop approvals.

An ASK verdict suspends the agent's tool call, opens an approval request on the
control plane, and waits. The agent is genuinely blocked for the duration —
that is the point — so everything here is bounded.

**Timeout denies** (ADR-017). An agent running unattended overnight must not be
able to obtain an ASK-worthy action by simply waiting for nobody to answer. The
cost is a stalled call that then fails cleanly, with a sentence saying why.

This is the one place in the proxy that deliberately blocks, which is why it is
the only place with a hard deadline measured in minutes rather than
milliseconds.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_TTL = 120.0
POLL_START = 0.5
POLL_MAX = 3.0


@dataclass
class ApprovalOutcome:
    approved: bool
    state: str              # approved | denied | expired | unavailable
    approval_id: str = ""
    detail: str = ""


class ApprovalClient:
    def __init__(self, api_url: str, ttl: float = DEFAULT_TTL, timeout: float = 5.0):
        self.api_url = api_url.rstrip("/")
        self.ttl = ttl
        self.timeout = timeout

    # -- transport ----------------------------------------------------------

    def _request(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.api_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if data is not None else "GET",
            headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    async def _call(self, path: str, payload: dict | None = None) -> dict | None:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._request, path, payload)
        except Exception:  # noqa: BLE001
            return None

    # -- flow ---------------------------------------------------------------

    async def _local(self, event, sentence: str) -> ApprovalOutcome | None:
        """Ask on screen (ADR-042). None when this machine cannot show a dialog."""
        from . import notify

        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, notify.ask, sentence, event.agent_id, event.tool.qualified, self.ttl)
        if answer is None:
            return None
        if answer:
            return ApprovalOutcome(True, "approved", detail="allowed by you on screen")
        return ApprovalOutcome(False, "denied", detail="blocked by you on screen, or no answer")

    async def request(self, event, sentence: str) -> ApprovalOutcome:
        """Ask on screen and on the dashboard at once; the first answer wins.

        Either channel alone is enough. With neither, nobody can say yes, so
        the answer is no (ADR-017)."""
        tasks = {asyncio.ensure_future(self._local(event, sentence)): "screen"}
        if self.api_url:
            tasks[asyncio.ensure_future(self._remote(event, sentence))] = "dashboard"
        pending = set(tasks)
        result: ApprovalOutcome | None = None
        while pending and result is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                out = t.result()
                # "could not ask" from one channel is not an answer; keep
                # waiting for the other.
                if out is not None and out.state != "unavailable":
                    result = out
                    break
                if out is not None and result is None and not pending:
                    result = out
        for t in pending:
            t.cancel()
        if result is None:
            return ApprovalOutcome(
                approved=False, state="unavailable",
                detail="no dashboard running and no way to ask on screen, so nobody could approve it")
        return result

    async def _remote(self, event, sentence: str) -> ApprovalOutcome:
        created = await self._call("/v1/approvals", {
            "event_id": event.event_id,
            "ttl_seconds": int(self.ttl),
            "reason": sentence,
            "agent_id": event.agent_id,
            "tool": event.tool.qualified,
            "resource": event.resource.uri,
            "resource_class": event.resource.cls.value,
        })
        if not created or "id" not in created:
            # The control plane is unreachable. We cannot ask a human, so we
            # cannot claim a human said yes.
            return ApprovalOutcome(
                approved=False, state="unavailable",
                detail="control plane unreachable, so no approval could be requested")

        approval_id = created["id"]
        deadline = asyncio.get_running_loop().time() + self.ttl
        delay = POLL_START

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, POLL_MAX)

            row = await self._call(f"/v1/approvals/{approval_id}")
            if not row:
                continue
            state = row.get("state", "pending")
            if state == "approved":
                return ApprovalOutcome(True, "approved", approval_id,
                                       f"approved by {row.get('decided_by') or 'a human'}")
            if state == "denied":
                return ApprovalOutcome(False, "denied", approval_id,
                                       f"denied by {row.get('decided_by') or 'a human'}")
            if state == "expired":
                break

        return ApprovalOutcome(
            False, "expired", approval_id,
            f"no one responded within {int(self.ttl)}s")
