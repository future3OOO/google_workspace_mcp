import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP
import os
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.utils import UserInputError
from gmail.gmail_tools import (
    delete_gmail_draft,
    draft_gmail_message,
    update_gmail_draft,
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
async def test_draft_gmail_message_rejects_unnamed_message_rfc822_base64_attachment():
    mock_service = Mock()

    with pytest.raises(UserInputError, match="No valid attachments were added"):
        await _unwrap(draft_gmail_message)(
            service=mock_service,
            user_google_email="user@example.com",
            to="recipient@example.com",
            subject="Attachment test",
            body="Please see attached.",
            attachments=[
                {
                    "content": base64.b64encode(b"Subject: Inner\r\n\r\nbody").decode(
                        "ascii"
                    ),
                    "mime_type": "message/rfc822",
                }
            ],
            include_signature=False,
        )

    assert not mock_service.users.return_value.drafts.return_value.create.called


@pytest.mark.asyncio
async def test_draft_gmail_message_treats_empty_reply_fields_as_missing_for_thread_fallback():
    mock_service = Mock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_reply"}
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message(
                "<msg1@example.com>",
                subject="Thread subject",
                from_value="Alice Example <alice@example.com>",
                reply_to="reply@example.com",
            )
        ]
    }

    await _unwrap(draft_gmail_message)(
        service=mock_service,
        user_google_email="user@example.com",
        to="",
        subject="Updated subject",
        body="Reply body",
        thread_id="thread123",
        in_reply_to="",
        references="",
        include_signature=False,
    )

    create_kwargs = (
        mock_service.users.return_value.drafts.return_value.create.call_args.kwargs
    )
    parsed = _parse_raw_message(create_kwargs["body"]["message"]["raw"])

    assert parsed["To"] == "reply@example.com"
    assert parsed["In-Reply-To"] == "<msg1@example.com>"
    assert parsed["References"] == "<msg1@example.com>"


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

    raw_message = update_kwargs["body"]["message"]["raw"]
    parsed = _parse_raw_message(raw_message)
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
        to="recipient@example.com",
        from_email="",
        in_reply_to="",
        references="",
        thread_id="thread123",
        attachments=[],
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert parsed["From"] is None
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_empty_to_and_from_name():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "thread123",
            "raw": _encode_raw_message(existing_message),
        }
    }
    mock_service.users().threads().get().execute.return_value = {
        "messages": [
            _thread_message(
                "<msg1@example.com>",
                from_value="Alice Example <alice@example.com>",
                reply_to="reply@example.com",
            )
        ]
    }

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        from_email="new-alias@example.com",
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert parsed["To"] is None
    assert parsed["From"] == "Existing Sender <new-alias@example.com>"


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_omitted_from_name_when_other_fields_are_explicit():
    mock_service = Mock()
    mock_service.users().drafts().update().execute.return_value = {"id": "draft123"}
    existing_message = EmailMessage(policy=SMTP)
    existing_message["Subject"] = "Old subject"
    existing_message["To"] = "recipient@example.com"
    existing_message["From"] = "Existing Sender <alias@example.com>"
    existing_message["In-Reply-To"] = "<msg1@example.com>"
    existing_message["References"] = "<root@example.com> <msg1@example.com>"
    existing_message.set_content("Old body")
    mock_service.users().drafts().get().execute.return_value = {
        "message": {
            "threadId": "thread123",
            "raw": _encode_raw_message(existing_message),
        }
    }
    mock_service.users.return_value.drafts.return_value.get.reset_mock()

    await _unwrap(update_gmail_draft)(
        service=mock_service,
        user_google_email="user@example.com",
        draft_id="draft123",
        to="recipient@example.com",
        cc="",
        bcc="",
        from_email="alias@example.com",
        thread_id="thread123",
        in_reply_to="<msg1@example.com>",
        references="<root@example.com> <msg1@example.com>",
        attachments=[],
        subject="Updated subject",
        body="Updated body",
        include_signature=False,
    )

    assert mock_service.users.return_value.drafts.return_value.get.call_count == 1

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])

    assert parsed["From"] == "Existing Sender <alias@example.com>"


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
        disposition="inline",
    )
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
async def test_update_gmail_draft_preserves_message_rfc822_attachment_when_omitted():
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
    existing_message.add_attachment(forwarded_message, filename="forwarded.eml")

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
        include_signature=False,
    )

    assert "Draft updated with 1 attachment(s)! Draft ID: draft123" in result

    update_kwargs = (
        mock_service.users.return_value.drafts.return_value.update.call_args.kwargs
    )
    parsed = _parse_raw_message(update_kwargs["body"]["message"]["raw"])
    preserved_parts = [
        part for part in parsed.walk() if part.get_filename() == "forwarded.eml"
    ]

    assert len(preserved_parts) == 1
    assert preserved_parts[0].get_content_type() == "message/rfc822"


@pytest.mark.asyncio
async def test_update_gmail_draft_preserves_unnamed_message_rfc822_attachment_when_omitted():
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
    existing_message.add_attachment(
        forwarded_message.as_bytes(policy=SMTP),
        maintype="message",
        subtype="rfc822",
    )

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
        and part.get_content_disposition() == "attachment"
    ]

    assert len(preserved_parts) == 1
    assert preserved_parts[0].get_filename() is None


@pytest.mark.asyncio
async def test_update_gmail_draft_rejects_missing_raw_message_content():
    mock_service = Mock()
    mock_service.users().drafts().get().execute.return_value = {"message": {}}

    with pytest.raises(UserInputError, match="Failed to retrieve raw message content"):
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
