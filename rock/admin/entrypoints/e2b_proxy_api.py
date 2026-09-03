from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from rock.admin.proto.e2b_connect import (
    CONNECT_CONTENT_TYPE,
    decode_connect_request,
    decode_unary_request,
    parse_keepalive_interval,
    sandbox_id_from_headers,
    start_command,
)
from rock.admin.proto.e2b_proxy_http import (
    MAX_E2B_WRITE_FIELDS,
    MAX_E2B_WRITE_FILES,
    E2BProxyAPIRoute,
    filesystem_connect_error_response,
    filesystem_rest_error_response,
    parse_write_entries,
    prepare_filesystem_unary,
    process_connect_error_response,
    read_connect_body,
    require_file_path,
    validate_rest_user,
)
from rock.admin.proto.request import E2BFilePathRequest, E2BListDirRequest
from rock.admin.proto.response import (
    E2BFileEntryResponse,
    E2BListDirResponse,
    E2BListedSandbox,
    E2BStatResponse,
    E2BWrittenFileResponse,
)
from rock.admin.service.e2b_proxy_service import E2BProxyService

e2b_proxy_router = APIRouter(route_class=E2BProxyAPIRoute)
e2b_proxy_service: E2BProxyService


def set_e2b_proxy_service(service: E2BProxyService) -> None:
    global e2b_proxy_service
    e2b_proxy_service = service


@e2b_proxy_router.get(
    "/v2/sandboxes",
    response_model=list[E2BListedSandbox],
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def list_sandboxes(
    metadata: Annotated[str, Query(min_length=1)],
) -> list[E2BListedSandbox]:
    return await e2b_proxy_service.list_sandboxes(metadata)


@e2b_proxy_router.post("/filesystem.Filesystem/MakeDir")
async def make_directory(request: Request) -> Response:
    try:
        sandbox_id, body = await prepare_filesystem_unary(request)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)
        payload = decode_unary_request(body, E2BFilePathRequest)
        await e2b_proxy_service.e2b_fs_make_dir(sandbox_id, payload.path)
        return JSONResponse(content={})
    except Exception as error:
        return filesystem_connect_error_response(error)


@e2b_proxy_router.post("/filesystem.Filesystem/ListDir")
async def list_directory(request: Request) -> Response:
    try:
        sandbox_id, body = await prepare_filesystem_unary(request)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)
        payload = decode_unary_request(body, E2BListDirRequest)
        entries = await e2b_proxy_service.e2b_fs_list(
            sandbox_id,
            payload.path,
            payload.effective_depth,
        )
        response = E2BListDirResponse(entries=[E2BFileEntryResponse.from_file_entry(entry) for entry in entries])
        return JSONResponse(content=response.model_dump(by_alias=True, exclude_none=True))
    except Exception as error:
        return filesystem_connect_error_response(error)


@e2b_proxy_router.post("/filesystem.Filesystem/Stat")
async def stat_file(request: Request) -> Response:
    try:
        sandbox_id, body = await prepare_filesystem_unary(request)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)
        payload = decode_unary_request(body, E2BFilePathRequest)
        entry = await e2b_proxy_service.e2b_fs_stat(sandbox_id, payload.path)
        response = E2BStatResponse(entry=E2BFileEntryResponse.from_file_entry(entry))
        return JSONResponse(content=response.model_dump(by_alias=True, exclude_none=True))
    except Exception as error:
        return filesystem_connect_error_response(error)


@e2b_proxy_router.get("/files")
async def read_file(
    request: Request,
    path: Annotated[str | None, Query()] = None,
    username: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        sandbox_id = sandbox_id_from_headers(request.headers)
        validate_rest_user(username)
        path = require_file_path(path)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)
        return await e2b_proxy_service.e2b_fs_read(sandbox_id, path)
    except Exception as error:
        return filesystem_rest_error_response(error)


@e2b_proxy_router.post("/files")
async def write_files(
    request: Request,
    path: Annotated[str | None, Query()] = None,
    username: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        sandbox_id = sandbox_id_from_headers(request.headers)
        validate_rest_user(username)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)

        async with request.form(max_files=MAX_E2B_WRITE_FILES, max_fields=MAX_E2B_WRITE_FIELDS) as form:
            entries = parse_write_entries(form.getlist("file"), path)
            written_paths = await e2b_proxy_service.e2b_fs_write_files(sandbox_id, entries)

        return JSONResponse(content=[E2BWrittenFileResponse.from_path(item).model_dump() for item in written_paths])
    except Exception as error:
        return filesystem_rest_error_response(error)


@e2b_proxy_router.post("/process.Process/Start")
async def start_process(request: Request) -> Response:
    try:
        sandbox_id = sandbox_id_from_headers(request.headers)
        payload = decode_connect_request(await read_connect_body(request))
        command = start_command(sandbox_id, payload, request.headers)
        keepalive_interval = parse_keepalive_interval(request.headers)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)
    except Exception as error:
        return process_connect_error_response(error)
    return StreamingResponse(
        e2b_proxy_service.start_process(command, keepalive_interval=keepalive_interval),
        media_type=CONNECT_CONTENT_TYPE,
    )


@e2b_proxy_router.get("/health")
async def health(request: Request) -> Response:
    sandbox_id = sandbox_id_from_headers(request.headers)
    status_code = 204 if await e2b_proxy_service.is_running(sandbox_id) else 502
    return Response(status_code=status_code)
