"""Gateway error type rendered as OpenAI-style error JSON."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code, self.code, self.message = status_code, code, message
        self.headers = dict(headers or {})
        if retry_after is not None:
            self.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))

    def body(self) -> dict:
        return {"error": {"type": self.code, "code": self.code, "message": self.message}}


async def gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)  # registered only for GatewayError
    headers = dict(exc.headers)
    rid = getattr(request.state, "request_id", None)
    if rid:
        headers["X-Request-ID"] = rid
    return JSONResponse(exc.body(), status_code=exc.status_code, headers=headers)
