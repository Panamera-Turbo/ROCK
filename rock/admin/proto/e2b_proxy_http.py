"""HTTP protocol support for the E2B sandbox proxy endpoints."""

from typing import cast

from fastapi import HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.datastructures import UploadFile as StarletteUploadFile

from rock.admin.proto.e2b_connect import (
    CONNECT_CONTENT_TYPE,
    CONNECT_ENVELOPE_HEADER_SIZE,
    CONNECT_REQUEST_LIMIT,
    encode_connect_end,
    sandbox_id_from_headers,
)
from rock.admin.proto.response import E2BConnectErrorPayload
from rock.logger import init_logger
from rock.sdk.common.exceptions import BadRequestRockError, E2BConnectError, SandboxNotFoundRockError

logger = init_logger(__name__)

MAX_E2B_WRITE_FILES = 256
MAX_E2B_WRITE_FIELDS = 16

_CONNECT_ERROR_STATUS = {
    "already_exists": 409,
    "deadline_exceeded": 504,
    "internal": 500,
    "invalid_argument": 400,
    "not_found": 404,
    "permission_denied": 403,
    "resource_exhausted": 429,
    "unavailable": 503,
    "unimplemented": 501,
}


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": status_code, "message": message})


def _connect_error_response(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=_CONNECT_ERROR_STATUS.get(code, 500),
        content={"code": code, "message": message},
    )


class E2BProxyAPIRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await route_handler(request)
            except RequestValidationError as error:
                message = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
                )
                return _error_response(400, message)
            except BadRequestRockError as error:
                logger.warning("E2B proxy request rejected: %s", error)
                return _error_response(400, str(error))
            except Exception:
                logger.exception("E2B proxy request failed")
                return _error_response(500, "Internal server error")

        return handler


def filesystem_connect_error_response(error: Exception) -> JSONResponse:
    if isinstance(error, SandboxNotFoundRockError | FileNotFoundError):
        return _connect_error_response("not_found", str(error))
    if isinstance(error, E2BConnectError):
        return _connect_error_response(error.code, error.message)
    if isinstance(error, BadRequestRockError | ValueError):
        return _connect_error_response("invalid_argument", str(error))
    if isinstance(error, PermissionError):
        return _connect_error_response("permission_denied", str(error))
    if isinstance(error, ConnectionError):
        return _connect_error_response("unavailable", str(error))
    logger.exception("E2B filesystem RPC failed", exc_info=error)
    return _connect_error_response("internal", "internal server error")


def filesystem_rest_error_response(error: Exception) -> JSONResponse:
    if isinstance(error, SandboxNotFoundRockError | FileNotFoundError):
        return _error_response(404, str(error))
    if isinstance(error, HTTPException):
        return _error_response(error.status_code, str(error.detail))
    if isinstance(error, BadRequestRockError | ValueError):
        return _error_response(400, str(error))
    if isinstance(error, PermissionError):
        return _error_response(403, str(error))
    if isinstance(error, ConnectionError):
        return _error_response(502, str(error))
    logger.exception("E2B filesystem HTTP request failed", exc_info=error)
    return _error_response(500, "Internal server error")


def process_connect_error_response(error: Exception) -> Response:
    if isinstance(error, SandboxNotFoundRockError):
        payload = E2BConnectErrorPayload(code="not_found", message=str(error))
    elif isinstance(error, BadRequestRockError):
        payload = E2BConnectErrorPayload(code="invalid_argument", message=str(error))
    elif isinstance(error, E2BConnectError):
        payload = E2BConnectErrorPayload(code=error.code, message=error.message)
    else:
        logger.exception("E2B process request preparation failed", exc_info=error)
        payload = E2BConnectErrorPayload(code="internal", message="internal server error")
    return Response(content=encode_connect_end(payload), media_type=CONNECT_CONTENT_TYPE)


async def read_connect_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > CONNECT_REQUEST_LIMIT + CONNECT_ENVELOPE_HEADER_SIZE:
            raise E2BConnectError("resource_exhausted", "Connect request is too large")
    return bytes(body)


async def read_unary_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > CONNECT_REQUEST_LIMIT:
            raise E2BConnectError("resource_exhausted", "Connect request is too large")
    return bytes(body)


async def prepare_filesystem_unary(request: Request) -> tuple[str, bytes]:
    sandbox_id = sandbox_id_from_headers(request.headers)
    return sandbox_id, await read_unary_body(request)


def validate_rest_user(username: str | None) -> None:
    if username not in (None, "user"):
        raise BadRequestRockError("only the default sandbox user is supported")


def require_file_path(path: str | None) -> str:
    if not path:
        raise BadRequestRockError("path is required")
    return path


def parse_write_entries(files: list[object], path: str | None) -> list[tuple[str, UploadFile]]:
    if not files:
        raise BadRequestRockError("at least one file part is required")

    entries: list[tuple[str, UploadFile]] = []
    for item in files:
        if not isinstance(item, StarletteUploadFile):
            raise BadRequestRockError("file form fields must contain uploaded files")
        target_path = path if path is not None else item.filename
        if not target_path:
            raise BadRequestRockError("file path is required")
        entries.append((target_path, cast(UploadFile, item)))
    return entries
