
from unittest.mock import MagicMock, patch
from email.message import EmailMessage

import ydb

with (
    patch.object(ydb, "Driver") as mock_driver,
    patch.object(ydb, "SessionPool") as mock_pool,
    patch.object(ydb.iam, "MetadataUrlCredentials"),
):
    import email_poller


# Создаём тестовое письмо
msg = EmailMessage()
msg["From"] = "client@example.com"
msg["To"] = "support@example.com"
msg["Subject"] = "Test"
msg.set_content("Where is my order?")

# Имитируем почтовый сервер
mock_imap = MagicMock()
mock_imap.__enter__.return_value = mock_imap
mock_imap.search.return_value = ("OK", [b"1"])
mock_imap.fetch.return_value = (
    "OK",
    [(b"1", msg.as_bytes())],
)

with (
    patch.object(email_poller.imaplib, "IMAP4_SSL", return_value=mock_imap),
    patch.object(email_poller, "get_iam_token", return_value="test-token"),
    patch.object(email_poller, "classify_with_llm", return_value="safe"),
    patch.object(email_poller, "call_agent") as mock_agent,
    patch.object(email_poller, "get_message_history") as mock_history,
    patch.object(email_poller, "save_message") as mock_save,
    patch.object(email_poller, "send_email") as mock_send,
):
    mock_agent.return_value = {
        "text": "Ваш заказ находится в обработке.",
        "ticket_id": None,
        "tokens_in": 10,
        "tokens_out": 8,
        "latency_ms": 100,
    }
    result = email_poller.process_emails()

    mock_agent.assert_called_once()
    mock_history.assert_called_once()
    assert mock_save.call_count == 2
    mock_send.assert_called_once()

    assert result == 1
    mock_imap.store.assert_called_once_with(
        b"1", "+FLAGS", "\\Seen"
    )

print("SECURITY TEST: PASS")
