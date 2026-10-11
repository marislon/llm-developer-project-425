import json
from unittest.mock import patch, MagicMock

from llm_input_guard import classify_with_llm


fake_response = {
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "approved"
                }
            ]
        }
    ]
}

mock_response = MagicMock()
mock_response.read.return_value = json.dumps(fake_response).encode("utf-8")

mock_context = MagicMock()
mock_context.__enter__.return_value = mock_response

with patch(
    "urllib.request.urlopen",
    side_effect=TimeoutError("Connection timeout")
):
    result = classify_with_llm(
        message="Как приготовить борщ?",
        iam_token="test-token",
        folder_id="test-folder"
    )

print("RESULT:", result)
assert result != "safe", "SECURITY ERROR: message was allowed"
print("SECURITY CHECK: PASS")
