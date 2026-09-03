import logging
import os
import time
import uuid
from pathlib import PurePosixPath

import pytest
from e2b import FileType, Sandbox, SandboxQuery, SandboxState

pytestmark = [pytest.mark.integration]

CONTROL_API_URL = os.getenv(
    "E2B_API_URL"
)
DATA_API_URL = os.getenv(
    "E2B_SANDBOX_URL"
)
TEMPLATE_ID = os.getenv(
    "E2B_TEMPLATE_ID"
)


def _api_options(api_key: str, request_id: str) -> dict:
    return {
        "api_url": CONTROL_API_URL,
        "api_key": api_key,
        "validate_api_key": False,
        "request_timeout": 60,
        "api_headers": {"X-Request-ID": request_id},
    }


def _kill_with_retry(sandbox_id: str, api_key: str, request_id: str) -> None:
    for attempt in range(3):
        try:
            Sandbox.kill(sandbox_id, **_api_options(api_key, request_id))
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1)


def _find_sandbox_ids(api_key: str, request_id: str) -> set[str]:
    paginator = Sandbox.list(
        query=SandboxQuery(metadata={"ap-job-id": request_id}),
        limit=100,
        **_api_options(api_key, request_id),
    )
    sandbox_ids = set()
    while paginator.has_next:
        sandbox_ids.update(item.sandbox_id for item in paginator.next_items())
    return sandbox_ids


def test_e2b_sdk_commands_run_across_write_and_read_domains(
    caplog: pytest.LogCaptureFixture,
    request: pytest.FixtureRequest,
):

    api_key = os.getenv("E2B_API_KEY")
    if not api_key:
        pytest.skip("E2B_API_KEY is required")

    caplog.set_level(logging.WARNING, logger="hpack")

    request_id = f"e2b-command-live-{uuid.uuid4().hex}"
    sandbox_ids: set[str] = set()

    def cleanup() -> None:
        if not sandbox_ids:
            try:
                sandbox_ids.update(_find_sandbox_ids(api_key, request_id))
            except Exception:
                logging.getLogger(__name__).exception("Failed to find live-test sandbox during cleanup")
        for sandbox_id in sandbox_ids:
            _kill_with_retry(sandbox_id, api_key, request_id)

    request.addfinalizer(cleanup)

    sandbox = Sandbox.create(
        template=TEMPLATE_ID,
        timeout=300,
        metadata={
            "ap-job-id": request_id,
            "e2b.agents.kruise.io/skip-init-runtime": "true",
            "e2b.agents.kruise.io/create-on-no-stock": "true",
            "e2b.agents.kruise.io/claim-timeout-seconds": "60",
            "e2b.agents.kruise.io/wait-ready-timeout-seconds": "60",
            "e2b.agents.kruise.io/reserve-failed-sandbox-for": "40",
            "e2b.agents.kruise.io/return-sandbox-ip": "true",
        },
        envs={"a": "123"},
        api_url=CONTROL_API_URL,
        sandbox_url=DATA_API_URL,
        api_key=api_key,
        validate_api_key=False,
        request_timeout=180,
        api_headers={"X-Request-ID": request_id},
    )
    sandbox_ids.add(sandbox.sandbox_id)

    info = sandbox.get_info()
    assert info.sandbox_id == sandbox.sandbox_id
    assert info.state == SandboxState.RUNNING

    result = sandbox.commands.run(
        'printf "rock-e2b:%s:%s\\n" "$a" "$COMMAND_ENV"; printf "rock-e2b-stderr\\n" >&2',
        envs={"COMMAND_ENV": "ok"},
        timeout=60,
        request_timeout=90,
    )

    assert result.exit_code == 0
    assert result.stdout == "rock-e2b:123:ok\n"
    assert result.stderr == "rock-e2b-stderr\n"

    files_root = PurePosixPath("/tmp") / request_id
    nested_dir = files_root / "nested"
    text_file = files_root / "single.txt"
    batch_text_file = nested_dir / "batch.txt"
    batch_bytes_file = nested_dir / "batch.bin"
    text_content = "hello from ROCK live filesystem\n"
    bytes_content = b"\x00\xffrock-live-bytes"

    assert sandbox.files.make_dir(str(nested_dir)) is True
    assert sandbox.files.make_dir(str(nested_dir)) is False

    written = sandbox.files.write(str(text_file), text_content)
    assert written.path == str(text_file)
    assert written.type is FileType.FILE

    batch_written = sandbox.files.write_files(
        [
            {"path": str(batch_text_file), "data": "batch text\n"},
            {"path": str(batch_bytes_file), "data": bytes_content},
        ]
    )
    assert [entry.path for entry in batch_written] == [str(batch_text_file), str(batch_bytes_file)]
    assert all(entry.type is FileType.FILE for entry in batch_written)

    assert sandbox.files.read(str(text_file), format="text") == text_content
    assert sandbox.files.read(str(batch_bytes_file), format="bytes") == bytearray(bytes_content)

    entries = {entry.path: entry for entry in sandbox.files.list(str(files_root), depth=2)}
    assert set(entries) == {
        str(nested_dir),
        str(text_file),
        str(batch_text_file),
        str(batch_bytes_file),
    }
    assert entries[str(nested_dir)].type is FileType.DIR
    assert entries[str(batch_bytes_file)].type is FileType.FILE

    file_info = sandbox.files.get_info(str(batch_bytes_file))
    assert file_info.path == str(batch_bytes_file)
    assert file_info.type is FileType.FILE
    assert file_info.size == len(bytes_content)

    directory_info = sandbox.files.get_info(str(nested_dir))
    assert directory_info.path == str(nested_dir)
    assert directory_info.type is FileType.DIR
