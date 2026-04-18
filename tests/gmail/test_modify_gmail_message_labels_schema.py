import inspect

from auth.scopes import GMAIL_COMPOSE_SCOPE
from core.server import server
from core.tool_registry import get_tool_components
from pydantic import TypeAdapter
import gmail.gmail_tools  # noqa: F401
from gmail.gmail_tools import update_gmail_draft


def test_modify_gmail_message_labels_optional_arrays_publish_array_type():
    components = get_tool_components(server)
    schema = components["modify_gmail_message_labels"].parameters["properties"]

    for field_name in ("add_label_ids", "remove_label_ids"):
        field_schema = schema[field_name]
        assert field_schema["type"] == "array"
        assert field_schema["items"] == {"type": "string"}
        assert field_schema["default"] is None


def test_batch_modify_gmail_message_labels_optional_arrays_publish_array_type():
    components = get_tool_components(server)
    schema = components["batch_modify_gmail_message_labels"].parameters["properties"]

    for field_name in ("add_label_ids", "remove_label_ids"):
        field_schema = schema[field_name]
        assert field_schema["type"] == "array"
        assert field_schema["items"] == {"type": "string"}
        assert field_schema["default"] is None


def test_gmail_draft_lifecycle_tools_are_registered():
    components = get_tool_components(server)

    assert "draft_gmail_message" in components
    assert "update_gmail_draft" in components
    assert "delete_gmail_draft" in components


def test_gmail_draft_lifecycle_tools_require_draft_id_for_mutation():
    components = get_tool_components(server)

    for tool_name in ("update_gmail_draft", "delete_gmail_draft"):
        schema = components[tool_name].parameters
        assert "draft_id" in schema["required"]
        assert schema["properties"]["draft_id"]["type"] == "string"


def test_gmail_draft_lifecycle_tools_use_compose_scope_not_send_scope():
    components = get_tool_components(server)

    for tool_name in (
        "draft_gmail_message",
        "update_gmail_draft",
        "delete_gmail_draft",
    ):
        assert components[tool_name].fn._required_google_scopes == [GMAIL_COMPOSE_SCOPE]


def test_update_gmail_draft_attachments_accept_json_encoded_array():
    fn = (
        update_gmail_draft.fn
        if hasattr(update_gmail_draft, "fn")
        else update_gmail_draft
    )
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__

    annotation = inspect.signature(fn).parameters["attachments"].annotation
    adapter = TypeAdapter(annotation)

    assert adapter.validate_python(
        '[{"filename": "doc.txt", "content": "aGVsbG8=", "mime_type": "text/plain"}]'
    ) == [{"filename": "doc.txt", "content": "aGVsbG8=", "mime_type": "text/plain"}]


def test_update_gmail_draft_schema_documents_replacement_contract():
    components = get_tool_components(server)
    schema = components["update_gmail_draft"].parameters
    properties = schema["properties"]

    assert "subject" in schema["required"]
    assert "body" in schema["required"]
    assert "Replacement" in properties["subject"]["description"]
    assert "Replacement" in properties["body"]["description"]

    attachments_description = properties["attachments"]["description"]
    assert "Omit or pass an empty list for no attachments" in attachments_description
    assert "invalid replacement attachment fails" in attachments_description
    assert "replaces" in attachments_description
