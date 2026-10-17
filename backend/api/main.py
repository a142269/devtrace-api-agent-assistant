"""DevTrace API: standalone developer-support service."""
import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import LockError

from api.auth import authenticated_user, bearer, issue_token, settings as auth_settings, verify_password
from core.skill_loader import SkillManager
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant, RuleDecisionModel
from developer.data import DOCS
from developer.events import event_sink
from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager
from memory.conversation_memory import MemoryManager

load_dotenv()
_developer = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _developer
    app.state.jwt_secret, app.state.users = auth_settings()
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
app.add_middleware(CORSMiddleware,
    allow_origins=[s.strip() for s in os.getenv('DEVTRACE_CORS_ORIGINS', 'http://localhost:5173').split(',') if s.strip()],
    allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'])

class DeveloperChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=120)

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)

def current_user(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> str:
    return authenticated_user(request, credentials)

async def limit(key: str, maximum: int) -> None:
    # INCR and expiry are atomic, and shared by all application workers.
    count = await _developer.redis.eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
        1, key)
    if count > maximum:
        raise HTTPException(429, '请求过于频繁，请稍后重试')

@app.post('/auth/login', tags=['认证'])
async def login(req: LoginRequest, request: Request):
    if _developer is None:
        raise HTTPException(503, '服务未就绪')
    client = request.client.host if request.client else 'unknown'
    await limit('developer:login-limit:' + hashlib.sha256(client.encode()).hexdigest(), 10)
    # Perform the same password derivation for unknown users to reduce account enumeration.
    encoded = request.app.state.users.get(req.username, 'pbkdf2_sha256$' + '00' * 16 + '$' + '00' * 32)
    if not verify_password(req.password, encoded) or req.username not in request.app.state.users:
        raise HTTPException(401, '用户名或密码错误')
    return {'access_token': issue_token(req.username, request.app.state.jwt_secret), 'token_type': 'bearer', 'expires_in': 3600}

@app.get("/health")
async def health():
    if _developer is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "service": "devtrace"}

@app.post("/developer/chat", tags=["开发者助手"])
async def developer_chat(req: DeveloperChatRequest, user_id: str = Depends(current_user),
                         idempotency_key: Optional[str] = Header(default=None, alias='Idempotency-Key', max_length=120)):
    if _developer is None:
        raise HTTPException(503, "开发者助手未就绪")
    await limit('developer:chat-limit:' + hashlib.sha256(user_id.encode()).hexdigest(), 30)
    import uuid
    conversation_id = req.conversation_id or str(uuid.uuid4())
    payload_hash = hashlib.sha256(json.dumps(req.model_dump(), sort_keys=True).encode()).hexdigest()
    idem = hashlib.sha256(f'{user_id}\0{idempotency_key}'.encode()).hexdigest() if idempotency_key else None
    idem_key = f'developer:request:{idem}' if idem else None
    locks = []
    try:
        # Lock the idempotency key first, then the session, so duplicate new-session requests serialize too.
        for key in ([f'developer:request-lock:{idem}'] if idem else []) + [
            'developer:session-lock:' + hashlib.sha256(f'{user_id}\0{conversation_id}'.encode()).hexdigest()
        ]:
            lock = _developer.redis.lock(key, timeout=180, blocking_timeout=0)
            if not await lock.acquire():
                raise HTTPException(409, '该请求或会话正在处理中，请稍后重试')
            locks.append(lock)
        if idem_key:
            cached = await _developer.redis.get(idem_key)
            if cached:
                record = json.loads(cached)
                if record['payload_hash'] != payload_hash:
                    raise HTTPException(409, '幂等键已用于其他请求')
                return record['response']
        try:
            result = await asyncio.wait_for(_developer.run(req.message, user_id, conversation_id), timeout=150)
        except asyncio.TimeoutError:
            raise HTTPException(504, '诊断超时，请使用同一会话重试')
        if idem_key:
            await _developer.redis.setex(idem_key, 86400, json.dumps({'payload_hash': payload_hash, 'response': result}, ensure_ascii=False))
        return result
    finally:
        for lock in reversed(locks):
            try:
                await lock.release()
            except LockError:
                pass

@app.get('/developer/runs', tags=['开发者助手'])
async def developer_runs(user_id: str = Depends(current_user)):
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    key = 'developer:run-index:' + hashlib.sha256(user_id.encode()).hexdigest()
    run_ids = await _developer.redis.zrevrange(key, 0, 49)
    items = []
    for run_id in run_ids:
        if isinstance(run_id, bytes):
            run_id = run_id.decode('utf-8')
        raw = await _developer.redis.get(f'developer:run:{run_id}')
        if not raw:
            continue
        record = json.loads(raw)
        if record.get('user_id') == user_id:
            response = record['response']
            items.append({'run_id': run_id, 'conversation_id': response['conversation_id'],
                          'kind': response['kind'], 'status': response['plan']['status'],
                          'goal': response['plan']['goal']})
    return {'runs': items}


@app.get("/developer/runs/{run_id}", tags=["开发者助手"])
async def developer_run(run_id: str, user_id: str = Depends(current_user)):
    if _developer is None:
        raise HTTPException(503, "开发者助手未就绪")
    raw = await _developer.redis.get(f"developer:run:{run_id}")
    if not raw:
        raise HTTPException(404, "运行记录不存在或已过期")
    record = json.loads(raw)
    if record.get("user_id") != user_id:
        raise HTTPException(404, "运行记录不存在或已过期")
    return record["response"]


@app.get('/developer/tasks/{task_id}', tags=['开发者助手'])
async def developer_task(task_id: str, user_id: str = Depends(current_user)):
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    raw = await _developer.redis.get(f'developer:task:{task_id}')
    record = json.loads(raw) if raw else None
    if not record or record.get('user_id') != user_id:
        raise HTTPException(404, '任务不存在或已过期')
    return {key: value for key, value in record.items() if key != 'user_id'}


@app.post('/developer/chat/stream', tags=['开发者助手'])
async def developer_chat_stream(req: DeveloperChatRequest, user_id: str = Depends(current_user),
                                idempotency_key: Optional[str] = Header(default=None, alias='Idempotency-Key', max_length=120)):
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    async def stream():
        import uuid
        task_id = str(uuid.uuid4())
        terminal = False
        queue: asyncio.Queue = asyncio.Queue()

        async def record_task(status, run_id=None):
            record = {'task_id': task_id, 'user_id': user_id, 'status': status, 'run_id': run_id}
            await _developer.redis.setex(f'developer:task:{task_id}', 86400, json.dumps(record))

        async def execute():
            nonlocal terminal
            reset_token = event_sink.set(lambda event: queue.put_nowait(('timeline', event)))
            try:
                result = await developer_chat(req, user_id, idempotency_key)
                await record_task('waiting' if result.get('plan', {}).get('status') == 'waiting' else 'completed', result.get('run_id'))
                terminal = True
                await queue.put(('complete', result))
            except HTTPException as exc:
                await record_task('failed')
                terminal = True
                await queue.put(('error', {'status': exc.status_code, 'detail': exc.detail}))
            except Exception:
                await record_task('failed')
                terminal = True
                await queue.put(('error', {'status': 500, 'detail': '诊断执行失败，请稍后重试'}))
            finally:
                event_sink.reset(reset_token)
                await queue.put(('end', {}))

        await record_task('running')
        task = asyncio.create_task(execute())
        try:
            yield f'event: started\ndata: {json.dumps({"task_id": task_id})}\n\n'
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n'
                    continue
                if kind == 'end':
                    break
                yield f'event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not terminal:
                await record_task('cancelled')
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
