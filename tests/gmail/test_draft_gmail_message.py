import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP
import os
import sys
from unittest.mock import Mock
from contextlib import asynccontextmanager

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

import gmail.gmail_tools as gmail_tools
from core.utils import UserInputError
from gmail.gmail_tools import (
    delete_gmail_draft,
    draft_gmail_message,
    send_gmail_message,
    update_gmail_draft,
    _resolve_url_attachments,
    _try_read_local_attachment,
)


def _unwrap(tool):
    """Unwrap FunctionTool + decorators to the original async function."""
    fn = tool.fn if hasattr(tool, "fn") else tool
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


def _thread_response(*message_ids):
    return {
        "messages": [
            {
                "payload": {
                    "headers": [{"name": "Message-ID", "value": message_id}],
                }
            }
            for message_id in message_ids
        ]
    }


def _encode_part(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def _thread_message(
    message_id: str,
    *,
    subject: str = "Meeting tomorrow",
    from_value: str = "sender@example.com",
    reply_to: str | None = None,
    to_value: str = "user@example.com",
    cc_value: str | None = None,
    date: str = "Fri, 28 Mar 2026 10:00:00 -0400",
    text: str | None = None,
    html: str | None = None,
):
    headers = [
        {"name": "Message-ID", "value": message_id},
        {"name": "Subject", "value": subject},
        {"name": "From", "value": from_value},
        {"name": "To", "value": to_value},
        {"name": "Date", "value": date},
    ]
    if reply_to:
        headers.append({"name": "Reply-To", "value": reply_to})
    if cc_value:
        headers.append({"name": "Cc", "value": cc_value})

    payload = {"headers": headers}
    parts = []
    if text is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _encode_part(text)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _encode_part(html)}})
    if parts:
        payload["mimeType"] = "multipart/alternative"
        payload["parts"] = parts

    return {"payload": payload}


class _FakeStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def aiter_bytes(self, chunk_size=0):
        for chunk in self._chunks:
            yield chunk


def _mock_stream_response(response):
    @asynccontextmanager
    async def _stream(_url, **_kwargs):
        yield response

    return _stream


def _parse_raw_message(raw_message: str):
    return BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw_message)
    )


def _encode_raw_message(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode()


@pytest.mark.asyncio
async def test_draft_gmail_message_reports_actual_attachment_count(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
    attachment_path = tmp_path / "sample.txt"
    attachment_path.write_text("hello attachment", encoding="utf-8")

    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft123"}

    result = await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Attachment test",
        body="Please see attached.",
        attachments=[{"path": str(attachment_path)}],
        include_signature=False,
    )

    assert "Draft created with 1 attachment(s)! Draft ID: draft123" in result

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_bytes = base64.urlsafe_b64decode(raw_message)

    assert b"Content-Disposition: attachment;" in raw_bytes
    assert b"sample.txt" in raw_bytes


@pytest.mark.asyncio
async def test_draft_gmail_message_raises_when_no_attachments_are_added(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
    missing_path = tmp_path / "missing.txt"

    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft123"}

    with pytest.raises(UserInputError, match="No valid attachments were added"):
        await _unwrap(draft_gmail_message)(
            service=mock_service,
            user_google_email="user@example.com",
            to="recipient@example.com",
            subject="Attachment test",
            body="Please see attached.",
            attachments=[{"path": str(missing_path)}],
            include_signature=False,
        )


@pytest.mark.asyncio
async def test_draft_gmail_message_surfaces_guidance_for_paths_outside_allowed_dirs(
    tmp_path, monkeypatch
):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    blocked_path = blocked_dir / "sample.txt"
    blocked_path.write_text("hello attachment", encoding="utf-8")
    monkeypatch.setenv("ALLOWED_FILE_DIRS", str(allowed_dir))

    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft123"}

    with pytest.raises(UserInputError) as exc_info:
        await _unwrap(draft_gmail_message)(
            service=mock_service,
            user_google_email="user@example.com",
            to="recipient@example.com",
            subject="Attachment test",
            body="Please see attached.",
            attachments=[{"path": str(blocked_path)}],
            include_signature=False,
        )

    message = str(exc_info.value)
    assert "No valid attachments were added" in message
    assert "permitted directories" in message
    assert "external mounts such as /run/media may be blocked" in message
    assert str(blocked_path) in message


@pytest.mark.asyncio
async def test_draft_gmail_message_appends_gmail_signature_html():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_sig"}
    mock_service.users().settings().sendAs().list().execute.return_value = {
        "sendAs": [
            {
                "sendAsEmail": "user@example.com",
                "isPrimary": True,
                "signature": "<div>Best,<br>Alice</div>",
            }
        ]
    }

    result = await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Signature test",
        body="<p>Hello</p>",
        body_format="html",
        include_signature=True,
    )

    assert "Draft created! Draft ID: draft_sig" in result

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_text = base64.urlsafe_b64decode(raw_message).decode("utf-8", errors="ignore")

    assert "<p>Hello</p>" in raw_text
    assert "Best,<br>Alice" in raw_text


@pytest.mark.asyncio
async def test_draft_gmail_message_builds_threaded_html_reply_as_multipart_alternative():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = _thread_response(
        "<msg1@example.com>",
        "<msg2@example.com>",
    )

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="<p>Thanks for the update.</p>",
        body_format="html",
        thread_id="thread123",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    parsed = _parse_raw_message(raw_message)

    assert parsed["Subject"] == "Re: Meeting tomorrow"
    assert parsed["To"] == "recipient@example.com"
    assert parsed["In-Reply-To"] == "<msg2@example.com>"
    assert parsed["References"] == "<msg1@example.com> <msg2@example.com>"
    assert parsed.get_content_type() == "multipart/alternative"
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == (
        "Thanks for the update."
    )
    assert parsed.get_body(preferencelist=("html",)).get_content().strip() == (
        "<p>Thanks for the update.</p>"
    )


@pytest.mark.asyncio
async def test_draft_gmail_message_builds_html_attachments_with_mixed_top_level():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {
        "id": "draft_attachments"
    }

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Attachment test",
        body="<p>Please see attached.</p>",
        body_format="html",
        attachments=[
            {
                "filename": "a.pdf",
                "content": "cGRmMQ==",
                "mime_type": "application/pdf",
            },
            {
                "filename": "b.pdf",
                "content": "cGRmMg==",
                "mime_type": "application/pdf",
            },
        ],
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    parsed = _parse_raw_message(raw_message)
    attachments = list(parsed.iter_attachments())

    assert parsed.get_content_type() == "multipart/mixed"
    assert parsed.get_body(preferencelist=("html",)).get_content().strip() == (
        "<p>Please see attached.</p>"
    )
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == (
        "Please see attached."
    )
    assert [attachment.get_filename() for attachment in attachments] == [
        "a.pdf",
        "b.pdf",
    ]


@pytest.mark.asyncio
async def test_draft_gmail_message_autofills_reply_recipient_from_thread_target():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message(
                "<msg1@example.com>",
                from_value="Alice Example <alice@example.com>",
                reply_to="reply@example.com",
            )
        ]
    }

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        subject="Meeting tomorrow",
        body="Thanks for the update.",
        thread_id="thread123",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    parsed = _parse_raw_message(create_kwargs["body"]["message"]["raw"])

    assert parsed["To"] == "reply@example.com"


@pytest.mark.asyncio
async def test_draft_gmail_message_fetches_thread_once_when_quoting_reply():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message(
                "<msg1@example.com>",
                from_value="Alice Example <alice@example.com>",
                text="Original plain text",
                html="<p>Original html</p>",
            )
        ]
    }
    mock_service.users.return_value.threads.return_value.get.reset_mock()

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="<p>Thanks for the update.</p>",
        body_format="html",
        thread_id="thread123",
        quote_original=True,
        include_signature=False,
    )

    assert mock_service.users.return_value.threads.return_value.get.call_count == 1
    thread_get_kwargs = (
        mock_service.users.return_value.threads.return_value.get.call_args.kwargs
    )
    assert thread_get_kwargs["format"] == "full"


@pytest.mark.asyncio
async def test_draft_gmail_message_autofills_reply_headers_from_thread():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = _thread_response(
        "<msg1@example.com>",
        "<msg2@example.com>",
        "<msg3@example.com>",
    )

    result = await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="Thanks for the update.",
        thread_id="thread123",
        include_signature=False,
    )

    # Verify threads().get() was called with correct parameters
    thread_get_kwargs = (
        mock_service.users.return_value.threads.return_value.get.call_args.kwargs
    )
    assert thread_get_kwargs["userId"] == "me"
    assert thread_get_kwargs["id"] == "thread123"
    assert thread_get_kwargs["format"] == "metadata"
    assert "Message-ID" in thread_get_kwargs["metadataHeaders"]

    assert "Draft created! Draft ID: draft_reply" in result

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_text = base64.urlsafe_b64decode(raw_message).decode("utf-8", errors="ignore")

    assert "In-Reply-To: <msg3@example.com>" in raw_text
    assert (
        "References: <msg1@example.com> <msg2@example.com> <msg3@example.com>"
        in raw_text
    )
    assert create_kwargs["body"]["message"]["threadId"] == "thread123"


@pytest.mark.asyncio
async def test_draft_gmail_message_uses_explicit_in_reply_to_when_filling_references():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = _thread_response(
        "<msg1@example.com>",
        "<msg2@example.com>",
        "<msg3@example.com>",
    )

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="Replying to an earlier message.",
        thread_id="thread123",
        in_reply_to="<msg2@example.com>",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_text = base64.urlsafe_b64decode(raw_message).decode("utf-8", errors="ignore")

    assert "In-Reply-To: <msg2@example.com>" in raw_text
    assert "References: <msg1@example.com> <msg2@example.com>" in raw_text
    assert "<msg3@example.com>" not in raw_text


@pytest.mark.asyncio
async def test_draft_gmail_message_uses_explicit_references_when_filling_in_reply_to():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = _thread_response(
        "<msg1@example.com>",
        "<msg2@example.com>",
        "<msg3@example.com>",
    )

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="Replying to an earlier message.",
        thread_id="thread123",
        references="<msg1@example.com> <msg2@example.com>",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_text = base64.urlsafe_b64decode(raw_message).decode("utf-8", errors="ignore")

    assert "In-Reply-To: <msg2@example.com>" in raw_text
    assert "References: <msg1@example.com> <msg2@example.com>" in raw_text
    assert "<msg3@example.com>" not in raw_text


@pytest.mark.asyncio
async def test_draft_gmail_message_gracefully_degrades_when_thread_fetch_fails():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.side_effect = RuntimeError("boom")

    result = await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="Thanks for the update.",
        thread_id="thread123",
        include_signature=False,
    )

    assert "Draft created! Draft ID: draft_reply" in result

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_text = base64.urlsafe_b64decode(raw_message).decode("utf-8", errors="ignore")

    assert "In-Reply-To:" not in raw_text
    assert "References:" not in raw_text


@pytest.mark.asyncio
async def test_draft_gmail_message_gracefully_degrades_when_thread_has_no_messages():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = {"messages": []}

    result = await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="Meeting tomorrow",
        body="Thanks for the update.",
        thread_id="thread123",
        include_signature=False,
    )

    assert "Draft created! Draft ID: draft_reply" in result

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_message = create_kwargs["body"]["message"]["raw"]
    raw_text = base64.urlsafe_b64decode(raw_message).decode("utf-8", errors="ignore")

    assert "In-Reply-To:" not in raw_text
    assert "References:" not in raw_text


# ---------------------------------------------------------------------------
# URL-based attachment tests
# ---------------------------------------------------------------------------


def test_try_read_local_attachment_reads_from_storage(tmp_path, monkeypatch):
    """MCP attachment URLs should be resolved from local storage."""
    import core.attachment_storage as storage_mod

    monkeypatch.setattr(storage_mod, "STORAGE_DIR", tmp_path)

    # Write a fake attachment file matching the naming convention.
    file_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (tmp_path / f"report_{file_id[:8]}.pdf").write_bytes(b"%PDF-fake")

    storage = storage_mod.AttachmentStorage()
    monkeypatch.setattr(storage_mod, "_attachment_storage", storage)

    # Manually register metadata so get_attachment_path works.
    from datetime import datetime, timedelta

    storage._metadata[file_id] = {
        "file_path": str(tmp_path / f"report_{file_id[:8]}.pdf"),
        "filename": "report.pdf",
        "mime_type": "application/pdf",
        "size": 9,
        "created_at": datetime.now(),
        "expires_at": datetime.now() + timedelta(hours=1),
    }

    result = _try_read_local_attachment(f"/attachments/{file_id}")
    assert result is not None
    data, filename, mime_type = result
    assert data == b"%PDF-fake"
    assert filename == "report.pdf"
    assert mime_type == "application/pdf"


def test_try_read_local_attachment_returns_none_for_non_attachment_url():
    """Non-attachment URLs should return None (fall through to HTTP fetch)."""
    assert _try_read_local_attachment("https://example.com/file.pdf") is None


def test_try_read_local_attachment_rejects_untrusted_origin(tmp_path, monkeypatch):
    file_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF-fake")

    storage = gmail_tools.get_attachment_storage()
    monkeypatch.setattr(
        storage,
        "get_attachment_metadata",
        lambda requested_file_id: (
            {
                "filename": "report.pdf",
                "mime_type": "application/pdf",
            }
            if requested_file_id == file_id
            else None
        ),
    )
    monkeypatch.setattr(
        storage,
        "get_attachment_path",
        lambda requested_file_id: file_path if requested_file_id == file_id else None,
    )
    monkeypatch.delenv("WORKSPACE_EXTERNAL_URL", raising=False)

    result = _try_read_local_attachment(f"https://evil.example/attachments/{file_id}")
    assert result is None


def test_try_read_local_attachment_requires_metadata_without_scan_fallback(
    tmp_path, monkeypatch
):
    file_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(gmail_tools, "STORAGE_DIR", tmp_path)
    (tmp_path / f"report_{file_id[:8]}.pdf").write_bytes(b"%PDF-fake")

    storage = gmail_tools.get_attachment_storage()
    monkeypatch.setattr(storage, "get_attachment_metadata", lambda _file_id: None)
    monkeypatch.setattr(storage, "get_attachment_path", lambda _file_id: None)

    result = _try_read_local_attachment(f"/attachments/{file_id}")
    assert result is None


def test_try_read_local_attachment_checks_file_size_before_reading(
    tmp_path, monkeypatch
):
    file_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    file_path = tmp_path / "large.bin"
    file_path.write_bytes(b"0123456789")

    storage = gmail_tools.get_attachment_storage()
    monkeypatch.setattr(
        storage,
        "get_attachment_metadata",
        lambda requested_file_id: (
            {
                "filename": "large.bin",
                "mime_type": "application/octet-stream",
            }
            if requested_file_id == file_id
            else None
        ),
    )
    monkeypatch.setattr(
        storage,
        "get_attachment_path",
        lambda requested_file_id: file_path if requested_file_id == file_id else None,
    )
    monkeypatch.setattr(gmail_tools, "MAX_EMAIL_ATTACHMENT_BYTES", 5)

    with pytest.raises(ValueError, match="Attachment exceeds 5 bytes"):
        _try_read_local_attachment(f"/attachments/{file_id}")


@pytest.mark.asyncio
async def test_resolve_url_attachments_fetches_external_url(monkeypatch):
    """External URLs should be fetched via streamed SSRF-safe download."""
    fake_response = _FakeStreamResponse(
        200,
        headers={"content-type": "application/pdf"},
        chunks=[b"file-", b"bytes"],
    )

    monkeypatch.setattr(
        gmail_tools, "ssrf_safe_stream", _mock_stream_response(fake_response)
    )

    attachments = [{"url": "https://example.com/report.pdf"}]
    resolved = await _resolve_url_attachments(attachments)

    assert len(resolved) == 1
    assert resolved[0]["_resolved_bytes"] == b"file-bytes"
    assert resolved[0]["filename"] == "report.pdf"
    assert resolved[0]["mime_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_resolve_url_attachments_preserves_non_url_entries():
    """Entries with path or content should pass through unchanged."""
    attachments = [
        {"path": "/some/file.txt"},
        {"content": "aGVsbG8=", "filename": "hello.txt"},
    ]
    resolved = await _resolve_url_attachments(attachments)
    assert resolved == attachments


@pytest.mark.asyncio
async def test_resolve_url_attachments_uses_provided_filename(monkeypatch):
    """User-specified filename should take precedence over URL-derived name."""
    fake_response = _FakeStreamResponse(
        200,
        headers={"content-type": "text/plain"},
        chunks=[b"data"],
    )

    monkeypatch.setattr(
        gmail_tools, "ssrf_safe_stream", _mock_stream_response(fake_response)
    )

    attachments = [{"url": "https://example.com/abc123", "filename": "my_report.txt"}]
    resolved = await _resolve_url_attachments(attachments)
    assert resolved[0]["filename"] == "my_report.txt"


@pytest.mark.asyncio
async def test_resolve_url_attachments_rejects_oversized(monkeypatch):
    """Attachments exceeding 25 MB should be skipped (passed through for error)."""
    big_data = b"x" * (26 * 1024 * 1024)
    fake_response = _FakeStreamResponse(
        200,
        headers={"content-type": "application/octet-stream"},
        chunks=[big_data],
    )

    monkeypatch.setattr(
        gmail_tools, "ssrf_safe_stream", _mock_stream_response(fake_response)
    )

    attachments = [{"url": "https://example.com/huge.bin"}]
    resolved = await _resolve_url_attachments(attachments)
    # Should pass through the original dict (no _resolved_bytes).
    assert "_resolved_bytes" not in resolved[0]
    assert resolved[0]["url"] == "https://example.com/huge.bin"


@pytest.mark.asyncio
async def test_draft_gmail_message_with_url_attachment(monkeypatch):
    """End-to-end: draft_gmail_message should accept a URL attachment."""
    fake_response = _FakeStreamResponse(
        200,
        headers={"content-type": "application/pdf"},
        chunks=[b"pdf-content-here"],
    )

    monkeypatch.setattr(
        gmail_tools, "ssrf_safe_stream", _mock_stream_response(fake_response)
    )

    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_url"}

    result = await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="URL attachment test",
        body="See attached from URL.",
        attachments=[{"url": "https://example.com/doc.pdf", "filename": "doc.pdf"}],
        include_signature=False,
    )

    assert "Draft created with 1 attachment(s)! Draft ID: draft_url" in result

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    raw_bytes = base64.urlsafe_b64decode(create_kwargs["body"]["message"]["raw"])
    assert b"Content-Disposition: attachment;" in raw_bytes
    assert b"doc.pdf" in raw_bytes


@pytest.mark.asyncio
async def test_send_gmail_message_with_url_attachment(monkeypatch):
    """End-to-end: send_gmail_message should accept a URL attachment."""
    fake_response = _FakeStreamResponse(
        200,
        headers={"content-type": "application/pdf"},
        chunks=[b"pdf-content-here"],
    )

    monkeypatch.setattr(
        gmail_tools, "ssrf_safe_stream", _mock_stream_response(fake_response)
    )

    mock_service = Mock()
    mock_service.users().messages().send().execute.return_value = {"id": "msg_url"}

    result = await _unwrap(send_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject="URL attachment test",
        body="See attached from URL.",
        attachments=[{"url": "https://example.com/doc.pdf", "filename": "doc.pdf"}],
    )

    assert "Email sent with 1 attachment(s)! Message ID: msg_url" in result

    create_kwargs = (
        mock_service.users.return_value.messages.return_value.send.call_args.kwargs
    )
    raw_bytes = base64.urlsafe_b64decode(create_kwargs["body"]["raw"])
    assert b"Content-Disposition: attachment;" in raw_bytes
    assert b"doc.pdf" in raw_bytes


@pytest.mark.asyncio
async def test_draft_gmail_message_treats_blank_reply_fields_like_omission():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message(
                "<msg1@example.com>", from_value="Alice <alice@example.com>"
            ),
            _thread_message(
                "<msg2@example.com>",
                from_value="Alice <alice@example.com>",
                reply_to="reply@example.com",
            ),
        ]
    }

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="",
        subject="Meeting tomorrow",
        body="Thanks for the update.",
        thread_id="thread123",
        in_reply_to="",
        references="",
        from_email="",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    parsed = _parse_raw_message(create_kwargs["body"]["message"]["raw"])

    assert parsed["From"] == "user@example.com"
    assert parsed["To"] == "reply@example.com"
    assert parsed["In-Reply-To"] == "<msg2@example.com>"
    assert parsed["References"] == "<msg1@example.com> <msg2@example.com>"


@pytest.mark.asyncio
async def test_draft_gmail_message_treats_whitespace_only_optional_fields_as_omitted_without_thread():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_blank"}

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to=" ",
        subject="Hello",
        body="Body",
        in_reply_to=" ",
        references=" ",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    parsed = _parse_raw_message(create_kwargs["body"]["message"]["raw"])

    assert "threadId" not in create_kwargs["body"]["message"]
    assert parsed["Subject"] == "Hello"
    assert parsed["To"] is None
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None


@pytest.mark.asyncio
async def test_draft_gmail_message_fetches_thread_when_subject_needs_fallback():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = {
        "messages": [_thread_message("<msg1@example.com>", subject="Thread subject")]
    }

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="recipient@example.com",
        subject=" ",
        body="Thanks for the update.",
        thread_id="thread123",
        in_reply_to="<msg1@example.com>",
        references="<msg1@example.com>",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    parsed = _parse_raw_message(create_kwargs["body"]["message"]["raw"])

    assert parsed["Subject"] == "Re: Thread subject"


@pytest.mark.asyncio
async def test_draft_gmail_message_rejects_quote_original_without_thread_id():
    mock_service = Mock()

    with pytest.raises(UserInputError, match="quote_original requires thread_id"):
        await _unwrap(draft_gmail_message)(
            service=mock_service,
            user_google_email="user@example.com",
            to="recipient@example.com",
            subject="Quote",
            body="Reply body",
            quote_original=True,
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.create.called


@pytest.mark.asyncio
async def test_draft_gmail_message_rejects_whitespace_only_thread_id_for_quote_original():
    mock_service = Mock()

    with pytest.raises(UserInputError, match="quote_original requires thread_id"):
        await _unwrap(draft_gmail_message)(
            service=mock_service,
            user_google_email="user@example.com",
            to="recipient@example.com",
            subject="Quote",
            body="Reply body",
            thread_id="   ",
            quote_original=True,
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.create.called


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_omitted_existing_draft_fields():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["Cc"] = "cc@example.com"
    existing_message["Bcc"] = "bcc@example.com"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message["In-Reply-To"] = "<msg1@example.com>"
    existing_message["References"] = "<root@example.com> <msg1@example.com>"
    existing_message.set_content("Old body")
    existing_message.add_attachment(
        b"existing attachment",
        maintype="text",
        subtype="plain",
        filename="existing.txt",
    )
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "thread123",
            "raw": _encode_raw_message(existing_message),
        }
    }

    result = await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id=" draft123 ",
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    get_kwargs = (
        mock_service.users.return_value.drafts.return_value.get.call_args.kwargs
    )
    assert get_kwargs == {"userId": "me", "id": "draft123", "format": "raw"}

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    assert update_kwargs["userId"] == "me"
    assert update_kwargs["id"] == "draft123"
    assert update_kwargs["body"]["message"]["threadId"] == "thread123"

    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    assert parsed["Subject"] == "Re: Updated subject"
    assert parsed["To"] == "recipient@example.com"
    assert parsed["Cc"] == "cc@example.com"
    assert parsed["Bcc"] == "bcc@example.com"
    assert parsed["From"] == "Existing Sender <alias@example.com>"
    assert parsed["In-Reply-To"] == "<msg1@example.com>"
    assert parsed["References"] == "<root@example.com> <msg1@example.com>"
    assert parsed.get_body(preferencelist=("plain",)).get_content().strip() == (
        "Updated body"
    )
    assert [attachment.get_filename() for attachment in parsed.iter_attachments()] == [
        "existing.txt"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "expected_in_reply_to", "expected_references"),
    [
        ({}, "<new-msg@example.com>", "<new-root@example.com> <new-msg@example.com>"),
        ({"in_reply_to": ""}, None, "<new-root@example.com> <new-msg@example.com>"),
        ({"references": ""}, "<new-msg@example.com>", None),
    ],
)
async def test_update_gmail_draft_rederives_reply_headers_when_thread_changes(
    updates, expected_in_reply_to, expected_references
):
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["In-Reply-To"] = "<old-msg@example.com>"
    existing_message["References"] = "<old-root@example.com> <old-msg@example.com>"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "old-thread",
            "raw": _encode_raw_message(existing_message),
        }
    }
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message("<new-root@example.com>", subject="New thread"),
            _thread_message("<new-msg@example.com>", subject="New thread"),
        ]
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        subject="Updated subject",
        body="Updated body",
        thread_id="new-thread",
        include_signature=False,
        **updates,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    assert update_kwargs["body"]["message"]["threadId"] == "new-thread"

    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    assert parsed["In-Reply-To"] == expected_in_reply_to
    assert parsed["References"] == expected_references


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_to", ["old-recipient@example.com", None])
async def test_update_gmail_draft_recomputes_recipient_when_thread_changes(existing_to):
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    if existing_to is not None:
        existing_message["To"] = existing_to
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "old-thread",
            "raw": _encode_raw_message(existing_message),
        }
    }
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message(
                "<new-msg@example.com>",
                from_value="New Sender <new-sender@example.com>",
                reply_to="new-reply@example.com",
            ),
        ]
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        subject="Updated subject",
        body="Updated body",
        thread_id="new-thread",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert parsed["To"] == "new-reply@example.com"
    assert parsed["In-Reply-To"] == "<new-msg@example.com>"


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_reply_to_when_from_email_is_explicit():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["From"] = "user@example.com"
    existing_message["Reply-To"] = "reply-target@example.com"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)},
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        subject="Updated subject",
        body="Updated body",
        from_email="user@example.com",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    assert parsed["Reply-To"] == "reply-target@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_email", "expected_from"),
    [("other@example.com", "Existing Sender <other@example.com>"), ("", None)],
)
async def test_update_gmail_draft_clears_reply_to_when_from_email_changes(
    from_email, expected_from
):
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message["Reply-To"] = "old-reply@example.com"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)},
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        subject="Updated subject",
        body="Updated body",
        from_email=from_email,
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert parsed["From"] == expected_from
    assert parsed["Reply-To"] is None


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_existing_html_body_format_when_omitted():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message.set_content("Old plain fallback")
    existing_message.add_alternative(
        "<html><body><p>Old body</p></body></html>", subtype="html"
    )
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "raw": _encode_raw_message(existing_message),
        }
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        subject="Updated subject",
        body="<html><body><p>Updated body</p></body></html>",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    html_body = parsed.get_body(preferencelist=("html",))
    plain_body = parsed.get_body(preferencelist=("plain",))

    assert html_body is not None
    assert "Updated body" in html_body.get_content()
    assert plain_body is not None
    assert plain_body.get_content().strip() == "Updated body"


@pytest.mark.asyncio
async def test_update_gmail_draft_clears_from_email_and_reply_headers_with_empty_strings():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message["In-Reply-To"] = "<m2@example.com>"
    existing_message["References"] = "<m1@example.com> <m2@example.com>"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "thread123",
            "raw": _encode_raw_message(existing_message),
        }
    }
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message("<m1@example.com>", subject="Thread subject"),
            _thread_message("<m2@example.com>", subject="Thread subject"),
        ]
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to=" ",
        from_email=" ",
        in_reply_to=" ",
        references=" ",
        thread_id=" ",
        attachments=[],
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert parsed["To"] is None
    assert parsed["From"] is None
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None


@pytest.mark.asyncio
async def test_update_gmail_draft_clears_reply_headers_when_thread_id_is_cleared():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["In-Reply-To"] = "<old-msg@example.com>"
    existing_message["References"] = "<old-root@example.com> <old-msg@example.com>"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "old-thread",
            "raw": _encode_raw_message(existing_message),
        }
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        subject="Updated subject",
        body="Updated body",
        thread_id="",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert "threadId" not in update_kwargs["body"]["message"]
    assert parsed["To"] == "recipient@example.com"
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None


@pytest.mark.asyncio
async def test_update_gmail_draft_resolves_url_attachments(monkeypatch):
    fake_response = _FakeStreamResponse(
        200,
        headers={"content-type": "application/pdf"},
        chunks=[b"pdf-content-here"],
    )
    monkeypatch.setattr(
        gmail_tools, "ssrf_safe_stream", _mock_stream_response(fake_response)
    )

    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "raw": _encode_raw_message(existing_message),
        }
    }

    result = await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        subject="Updated subject",
        body="Updated body",
        attachments=[{"url": "https://example.com/doc.pdf", "filename": "doc.pdf"}],
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    raw_bytes = base64.urlsafe_b64decode(update_kwargs["body"]["message"]["raw"])
    assert b"Content-Disposition: attachment;" in raw_bytes
    assert b"doc.pdf" in raw_bytes


@pytest.mark.asyncio
async def test_update_gmail_draft_rejects_partial_attachment_replacements(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ALLOWED_FILE_DIRS", str(tmp_path))
    valid_path = tmp_path / "new.txt"
    valid_path.write_text("replacement", encoding="utf-8")
    missing_path = tmp_path / "missing.txt"

    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message.set_content("Old body")
    existing_message.add_attachment(
        b"existing attachment",
        maintype="text",
        subtype="plain",
        filename="existing.txt",
    )
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)}
    }
    mock_service.users.return_value.drafts.return_value.update.reset_mock()

    with pytest.raises(UserInputError, match="Attachment replacement failed"):
        await _unwrap(update_gmail_draft)(
            service=mock_service,
            user_google_email="user@example.com",
            draft_id="draft123",
            to="recipient@example.com",
            subject="Updated subject",
            body="Updated body",
            attachments=[{"path": str(valid_path)}, {"path": str(missing_path)}],
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.update.called


@pytest.mark.asyncio
async def test_update_gmail_draft_rejects_preserved_attachment_errors(monkeypatch):
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)}
    }
    mock_service.users.return_value.drafts.return_value.update.reset_mock()
    monkeypatch.setattr(
        gmail_tools,
        "_extract_preserved_attachments",
        lambda _message: [
            {
                "filename": "bad.txt",
                "content": "not-valid-base64***",
                "mime_type": "text/plain",
            },
            {
                "filename": "good.txt",
                "content": "Z29vZA==",
                "mime_type": "text/plain",
            },
        ],
    )

    with pytest.raises(
        UserInputError, match="Existing draft attachments could not be preserved"
    ):
        await _unwrap(update_gmail_draft)(
            service=mock_service,
            user_google_email="user@example.com",
            draft_id="draft123",
            subject="Updated subject",
            body="Updated body",
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.update.called


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_zero_byte_attachment_when_omitted():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message.set_content("Old body")
    existing_message.add_attachment(
        b"",
        maintype="application",
        subtype="octet-stream",
        filename="empty.bin",
    )
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)}
    }

    result = await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    attachments = list(parsed.iter_attachments())

    assert len(attachments) == 1
    assert attachments[0].get_filename() == "empty.bin"
    assert attachments[0].get_payload(decode=True) == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "expected_filename"),
    [("forwarded.eml", "forwarded.eml"), (None, None)],
)
async def test_update_gmail_draft_preserves_message_rfc822_attachment_when_omitted(
    filename, expected_filename
):
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message.set_content("Old body")

    forwarded_message = EmailMessage(policy=SMTP)
    forwarded_message["Subject"] = "Forwarded subject"
    forwarded_message["From"] = "sender@example.com"
    forwarded_message["To"] = "recipient@example.com"
    forwarded_message.set_content("Forwarded body")
    forwarded_message.add_attachment(
        b"INNERDATA",
        maintype="application",
        subtype="pdf",
        filename="inner.pdf",
    )
    if filename is None:
        existing_message.add_attachment(
            forwarded_message.as_bytes(policy=SMTP),
            maintype="message",
            subtype="rfc822",
        )
    else:
        existing_message.add_attachment(forwarded_message, filename=filename)

    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)}
    }

    result = await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    preserved_parts = [
        part
        for part in parsed.walk()
        if part.get_content_type() == "message/rfc822"
        and (
            part.get_filename() == expected_filename
            if expected_filename is not None
            else part.get_content_disposition() == "attachment"
        )
    ]

    assert len(preserved_parts) == 1
    assert preserved_parts[0].get_filename() == expected_filename
    assert preserved_parts[0]["Content-Transfer-Encoding"] != "base64"
    assert [attachment.get_filename() for attachment in parsed.iter_attachments()] == [
        expected_filename
    ]


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_multipart_attachment_when_omitted():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message.set_content("Old body")

    multipart_attachment = EmailMessage(policy=SMTP)
    multipart_attachment.set_content("Nested plain")
    multipart_attachment.add_alternative("<p>Nested html</p>", subtype="html")
    multipart_attachment.add_header(
        "Content-Disposition", "attachment", filename="bundle.eml"
    )
    existing_message.make_mixed()
    existing_message.attach(multipart_attachment)
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)}
    }

    result = await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    attachments = list(parsed.iter_attachments())

    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "multipart/alternative"
    assert attachments[0].is_multipart()
    assert attachments[0].get_filename() == "bundle.eml"
    assert [part.get_content_type() for part in attachments[0].iter_parts()] == [
        "text/plain",
        "text/html",
    ]


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_inline_related_parts_when_attachments_omitted():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message.set_content("Plain fallback")
    existing_message.add_alternative(
        '<html><body><p>Old body</p><img src="cid:logo"></body></html>',
        subtype="html",
    )
    existing_message.get_body(preferencelist=("html",)).add_related(
        b"PNGDATA",
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="logo.png",
    )
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": _encode_raw_message(existing_message)}
    }

    result = await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        subject="Updated subject",
        body='<html><body><p>Updated body</p><img src="cid:logo"></body></html>',
        body_format="html",
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    preserved_parts = [
        part for part in parsed.walk() if part.get("Content-ID") == "<logo>"
    ]

    assert len(preserved_parts) == 1
    assert preserved_parts[0].get_filename() == "logo.png"
    assert preserved_parts[0].get_content_disposition() == "inline"


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_cid_attachment_as_regular_attachment():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["In-Reply-To"] = "<m2@example.com>"
    existing_message.set_content("Plain fallback")
    existing_message.add_alternative(
        "<html><body><p>Old body</p></body></html>", subtype="html"
    )
    existing_message.add_attachment(
        b"PNGDATA",
        maintype="image",
        subtype="png",
        cid="<logo>",
        filename="logo.png",
        disposition="attachment",
    )
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "thread123",
            "raw": _encode_raw_message(existing_message),
        }
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        subject="Updated subject",
        body="<html><body><p>Updated body</p></body></html>",
        body_format="html",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    attachments = list(parsed.iter_attachments())

    assert len(attachments) == 1
    assert parsed["From"] is None
    assert parsed["In-Reply-To"] == "<m2@example.com>"
    assert parsed["References"] is None
    assert attachments[0].get_filename() == "logo.png"
    assert attachments[0].get_content_disposition() == "attachment"
    assert attachments[0].get("Content-ID") == "<logo>"


@pytest.mark.asyncio
async def test_update_gmail_draft_rejects_malformed_raw_message_content():
    mock_service = Mock()
    mock_service.users().drafts().get().execute.return_value = {
        "message": {"raw": "not-valid-base64***"}
    }

    with pytest.raises(
        UserInputError, match="Failed to parse draft 'draft123' raw MIME content"
    ):
        await _unwrap(update_gmail_draft)(
            service=mock_service,
            user_google_email="user@example.com",
            draft_id="draft123",
            subject="Updated subject",
            body="Updated body",
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.update.called


@pytest.mark.asyncio
async def test_update_gmail_draft_rejects_blank_draft_id():
    mock_service = Mock()

    with pytest.raises(UserInputError, match="draft_id is required"):
        await _unwrap(update_gmail_draft)(
            service=mock_service,
            user_google_email="user@example.com",
            draft_id=" ",
            to="recipient@example.com",
            subject="Updated subject",
            body="Updated body",
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.update.called


@pytest.mark.asyncio
async def test_delete_gmail_draft_deletes_existing_draft():
    mock_service = Mock()

    result = await _unwrap(delete_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id=" draft123 ",
    )

    assert result == "Draft deleted! Draft ID: draft123"

    delete_kwargs = (
        mock_service.users.return_value.drafts.return_value.delete.call_args.kwargs
    )
    assert delete_kwargs == {"userId": "me", "id": "draft123"}


@pytest.mark.asyncio
async def test_delete_gmail_draft_rejects_blank_draft_id():
    mock_service = Mock()

    with pytest.raises(UserInputError, match="draft_id is required"):
        await _unwrap(delete_gmail_draft)(
            service=mock_service,
            user_google_email="user@example.com",
            draft_id=" ",
        )

    assert not mock_service.users.return_value.drafts.return_value.delete.called
