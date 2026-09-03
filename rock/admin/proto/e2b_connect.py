import json
import math
from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from rock.admin.proto.request import E2BStartRequest, SandboxCommand
from rock.admin.proto.response import E2BConnectEndResponse, E2BConnectErrorPayload, E2BProcessResponse
from rock.sdk.common.exceptions import BadRequestRockError, E2BConnectError

CONNECT_CONTENT_TYPE = "application/connect+json"
CONNECT_REQUEST_LIMIT = 1024 * 1024
CONNECT_ENVELOPE_HEADER_SIZE = 5
MAX_COMMAND_TIMEOUT_MS = 85_000
RequestModel = TypeVar("RequestModel", bound=BaseModel)


def sandbox_id_from_headers(headers: Mapping[str, str]) -> str:
    sandbox_id = headers.get("e2b-sandbox-id", "").strip()
    if not sandbox_id:
        raise BadRequestRockError("E2b-Sandbox-Id is required")
    return sandbox_id


def parse_keepalive_interval(headers: Mapping[str, str]) -> float:
    try:
        interval = float(headers.get("keepalive-ping-interval", "50"))
    except ValueError as error:
        raise BadRequestRockError("Keepalive-Ping-Interval must be numeric") from error
    if not math.isfinite(interval) or not 0.01 <= interval <= 60:
        raise BadRequestRockError("Keepalive-Ping-Interval is outside the supported range")
    return interval


def decode_connect_request(body: bytes) -> E2BStartRequest:
    if len(body) < CONNECT_ENVELOPE_HEADER_SIZE:
        raise BadRequestRockError("truncated Connect envelope")
    flags = body[0]
    size = int.from_bytes(body[1:CONNECT_ENVELOPE_HEADER_SIZE], "big")
    if flags != 0:
        raise BadRequestRockError("unsupported Connect request flags")
    if size > CONNECT_REQUEST_LIMIT:
        raise E2BConnectError("resource_exhausted", "Connect request is too large")
    if len(body) != size + CONNECT_ENVELOPE_HEADER_SIZE:
        raise BadRequestRockError("invalid Connect envelope length")
    try:
        payload = json.loads(body[CONNECT_ENVELOPE_HEADER_SIZE:])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BadRequestRockError("invalid Connect JSON payload") from error
    try:
        return E2BStartRequest.model_validate(payload)
    except ValidationError as error:
        raise BadRequestRockError("invalid Connect request payload") from error


def decode_unary_request(body: bytes, model: type[RequestModel]) -> RequestModel:
    if len(body) > CONNECT_REQUEST_LIMIT:
        raise E2BConnectError("resource_exhausted", "Connect request is too large")
    try:
        return model.model_validate_json(body)
    except (ValidationError, ValueError) as error:
        raise BadRequestRockError("invalid Connect JSON payload") from error


def encode_connect_message(payload: E2BProcessResponse) -> bytes:
    encoded = payload.model_dump_json(by_alias=True, exclude_none=True).encode()
    return b"\x00" + len(encoded).to_bytes(4, "big") + encoded


def encode_connect_end(error: E2BConnectErrorPayload | None = None) -> bytes:
    payload = E2BConnectEndResponse(error=error)
    encoded = payload.model_dump_json(exclude_none=True).encode()
    return b"\x02" + len(encoded).to_bytes(4, "big") + encoded


def _command_timeout(headers: Mapping[str, str]) -> float:
    raw_timeout = headers.get("connect-timeout-ms")
    if raw_timeout is None or not raw_timeout.strip():
        raise E2BConnectError(
            "unimplemented",
            "unlimited command timeout is not supported; use a timeout of at most 60 seconds",
        )
    try:
        timeout_ms = int(raw_timeout)
    except ValueError as error:
        raise BadRequestRockError("Connect-Timeout-Ms must be an integer") from error
    if timeout_ms < 0:
        raise BadRequestRockError("Connect-Timeout-Ms must be non-negative")
    if timeout_ms == 0 or timeout_ms > MAX_COMMAND_TIMEOUT_MS:
        raise E2BConnectError(
            "unimplemented",
            "only command timeouts between 1 and 60000 milliseconds are supported",
        )
    return timeout_ms / 1000


def start_command(
    sandbox_id: str,
    payload: E2BStartRequest,
    headers: Mapping[str, str],
) -> SandboxCommand:
    if payload.stdin:
        raise E2BConnectError("unimplemented", "interactive stdin is not supported")
    if payload.pty is not None:
        raise E2BConnectError("unimplemented", "PTY commands are not supported")
    if payload.tag is not None:
        raise E2BConnectError("unimplemented", "tagged commands are not supported")

    process = payload.process
    return SandboxCommand(
        sandbox_id=sandbox_id,
        command=[process.cmd, *process.args],
        timeout=_command_timeout(headers),
        shell=False,
        check=False,
        env=process.envs or None,
        cwd=process.cwd or None,
    )
