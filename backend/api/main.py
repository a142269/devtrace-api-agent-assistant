"""DevTrace API: standalone developer-support service."""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from core.skill_loader import SkillManager
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager
from memory.conversation_memory import MemoryManager

load_dotenv()
_developer = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _developer
    mode = os.getenv("DEVELOPER_MODEL_MODE", "anthropic")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if mode != "rules" and not key:
        raise RuntimeError("请设置 ANTHROPIC_API_KEY，或使用 DEVELOPER_MODEL_MODE=rules")
    model_name = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip()
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
    skills = SkillManager(str(Path(__file__).resolve().parent.parent / "developer" / "skills"))
    skills.load()
    memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        api_key=key, base_url=base_url, model=model_name,
        namespace="developer", episodic_collection="developer_episodic",
        profile_collection="developer_profile",
    )
    knowledge = KnowledgeBase(
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        collection_name="developer_knowledge", default_documents=DOCS,
    )
    model = RuleDecisionModel() if mode == "rules" else AnthropicDecisionModel(
        key, model_name, base_url, skills.prompt_for("API 401 429 webhook", "developer"))
    _developer = DeveloperAssistant(
        memory=memory, knowledge=knowledge, redis_client=memory._redis, model=model,
        tool_manager=MCPToolManager(api_key=key, base_url=base_url, model=model_name),
        sandbox_url=os.getenv("DEVELOPER_SANDBOX_URL", "http://developer-sandbox:8100"),
    )
    try:
        yield
    finally:
        await memory.close()
        _developer = None

app = FastAPI(title="DevTrace 开发者平台 API 接入 Agent 助手", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class DeveloperChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="anonymous", min_length=1, max_length=120)
    conversation_id: Optional[str] = Field(default=None, max_length=120)

@app.get("/health")
async def health():
    if _developer is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "service": "devtrace"}

@app.post("/developer/chat", tags=["开发者助手"])
async def developer_chat(req: DeveloperChatRequest):
    if _developer is None:
        raise HTTPException(503, "开发者助手未就绪")
    return await _developer.run(req.message, req.user_id, req.conversation_id)

@app.get("/developer/runs/{run_id}", tags=["开发者助手"])
async def developer_run(run_id: str, user_id: str):
    if _developer is None:
        raise HTTPException(503, "开发者助手未就绪")
    raw = await _developer.redis.get(f"developer:run:{run_id}")
    if not raw:
        raise HTTPException(404, "运行记录不存在或已过期")
    record = json.loads(raw)
    if record.get("user_id") != user_id:
        raise HTTPException(404, "运行记录不存在或已过期")
    return record["response"]
