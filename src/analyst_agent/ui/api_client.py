"""The UI's only way to reach the system.

The interface talks to the API and never to the database. That boundary is worth keeping strict:
it means the UI cannot accidentally show something the API would not, and that everything on
screen went through the same authorisation and shaping as any other client would get.
"""

from __future__ import annotations

from typing import Any

import requests

from analyst_agent.config import get_settings

TIMEOUT = 20


class ApiError(RuntimeError):
    """The API said no, and this is what it said."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class AnalystApi:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or get_settings().api_base_url).rstrip("/")

    # --- plumbing --------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(
            method, f"{self.base_url}{path}", timeout=TIMEOUT, **kwargs
        )
        if response.status_code >= 400:
            detail = response.text
            try:
                body = response.json()
                detail = body.get("detail") or body.get("error") or detail
            except ValueError:
                pass
            raise ApiError(response.status_code, str(detail))
        return response.json() if response.content else None

    # --- calls -----------------------------------------------------------

    def healthy(self) -> bool:
        try:
            self._request("GET", "/healthz")
            return True
        except Exception:
            return False

    def ask(self, question: str, requested_by: str | None = None) -> dict[str, Any]:
        return self._request(
            "POST", "/v1/questions", json={"question": question, "requested_by": requested_by}
        )

    def run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def trace(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/trace")

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/runs?limit={limit}")

    def metrics(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/metrics")

    def schema(self) -> dict[str, Any]:
        return self._request("GET", "/v1/schema")

    def decide(
        self, run_id: str, approval_id: str, approve: bool, decided_by: str, reason: str | None
    ) -> dict[str, Any]:
        action = "approve" if approve else "reject"
        return self._request(
            "POST",
            f"/v1/runs/{run_id}/approvals/{approval_id}/{action}",
            json={"decided_by": decided_by, "reason": reason},
        )

    def answer(self, run_id: str, answer: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/runs/{run_id}/answer", json={"answer": answer})
