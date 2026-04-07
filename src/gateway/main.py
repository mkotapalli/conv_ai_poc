from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from gateway_app.service import GatewayService

SERVICE = GatewayService()
app = FastAPI(title="AgentCore Gateway", version="0.1.0")


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1)


ChatRequest.model_rebuild()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "gateway",
        "message": "Use POST /chat to send a request through the AgentCore gateway.",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return SERVICE.health()


@app.post("/chat")
def chat(payload: ChatRequest, authorization: str | None = Header(default=None)) -> dict:
    bearer_token = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization.split(" ", 1)[1].strip()

    try:
        user_context = SERVICE.verify_okta_token(bearer_token)
    except Exception as exc:  # pragma: no cover - real auth failures are environment-driven
        raise HTTPException(status_code=401, detail=f"Authentication failed: {exc}") from exc

    return SERVICE.forward_to_orchestrator(payload.model_dump(), user_context)
