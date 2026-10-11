
import os
import json
import urllib.request
import urllib.error


FOLDER_ID = os.environ.get("YC_FOLDER_ID")
IAM_TOKEN = os.environ.get("YC_IAM_TOKEN")

if not FOLDER_ID or not IAM_TOKEN:
    raise RuntimeError(
        "Для теста нужны YC_FOLDER_ID и YC_IAM_TOKEN"
    )

payload = {
    "model": f"gpt://{FOLDER_ID}/yandexgpt/latest",
    "instructions": (
        "Ты — помощник образовательной платформы. "
        "Перед ответом обязательно используй file_search. "
        "Отвечай только по найденным документам. "
        "Если ответа нет, прямо скажи об этом."
    ),
    "input": "Как отменить подписку на образовательной платформе?",
    "tools": [
        {
            "type": "file_search",
            "vector_store_ids": ["fvtdsn8q2t9elir34u5h"],
        }
    ],
}

request = urllib.request.Request(
    "https://ai.api.cloud.yandex.net/v1/responses",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {IAM_TOKEN}",
        "Content-Type": "application/json",
    },
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))

    print("STATUS:", result.get("status"))

    for item in result.get("output", []):
        print("EVENT:", item.get("type"))

        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    print("ANSWER:", content.get("text"))

except urllib.error.HTTPError as error:
    print("HTTP ERROR:", error.code)
    print(error.read().decode("utf-8"))
