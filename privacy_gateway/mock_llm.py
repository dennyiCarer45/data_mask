"""Local OpenAI-compatible echo target used behind the real LiteLLM proxy."""

import time

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(docs_url=None, redoc_url=None)


class Completion(BaseModel):
    messages: list[dict]
    model: str


@app.post("/v1/chat/completions")
async def completion(body: Completion):
    content = "\n".join(m["content"] for m in body.messages)
    return {
        "id": "chatcmpl-local-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "local-mock",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
