from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field
from a2a.types import AgentSkill
from strands.multiagent.a2a import A2AServer

from acct_mgmt_agent.service import AccountManagementService

SERVICE = AccountManagementService()


def build_app() -> FastAPI:
    base_app = FastAPI(title="Account Management Agent", version="0.1.0")

    a2a_enabled = SERVICE.settings.get_bool("service.a2a.enabled", True)
    if a2a_enabled:
        a2a_agent = SERVICE.get_a2a_agent()
        if a2a_agent is not None:
            public_url = SERVICE.settings.get("service.a2a.public_url", "").strip()
            host = SERVICE.settings.get("server.host", "0.0.0.0")
            port = int(SERVICE.settings.get("server.port", "8080"))
            skills = [
                AgentSkill(
                    id="password_reset_unlock",
                    name="Account Recovery",
                    description="Handle password reset and account unlock requests for employees.",
                    tags=["account", "password", "unlock", "reset"],
                    examples=["I forgot my password", "Unlock my corporate account"],
                    inputModes=["text"],
                    outputModes=["text"],
                )
            ]
            a2a_server = A2AServer(
                a2a_agent,
                host=host,
                port=port,
                http_url=public_url or None,
                version=SERVICE.settings.get("app.version", "0.1.0"),
                skills=skills,
            )
            a2a_app = a2a_server.to_fastapi_app()
            base_app.mount(SERVICE.settings.get("service.a2a.mount_path", "/a2a"), a2a_app)

    return base_app


app = build_app()


class AgentRequest(BaseModel):
    genesys_conversation_id: str | None = None
    gecx_session_id: str | None = None
    aie_session_id: str | None = None
    request_type: str = Field(min_length=1)
    member_eid: str | None = None
    delivery_type: str | None = None
    intent: str | None = None


AgentRequest.model_rebuild()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "acct-mgmt-agent",
        "message": "Use POST /assist for account validation, password-link delivery, and end-session responses.",
    }


@app.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    return SERVICE.health()


@app.post("/assist")
def assist(payload: AgentRequest) -> dict:
    return SERVICE.handle_request(payload.model_dump())


@app.post("/invocations")
def invocations(payload: AgentRequest) -> dict: 
    return SERVICE.handle_request(payload.model_dump())
