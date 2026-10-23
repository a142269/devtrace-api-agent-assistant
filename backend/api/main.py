"""DevTrace API: standalone developer-support service."""
import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import LockError, RedisError

from api.auth import (authenticated_user, bearer, issue_token,
                      require_admin, settings as auth_settings, verify_password, workspace_for)
from core.skill_loader import SkillManager
from developer import audit
from developer import task_queue
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant, RuleDecisionModel, _redact
from developer.data import DOCS
from developer.events import event_sink
from developer.public_knowledge import PublicDocsKnowledge
from developer.prompts import describe as describe_prompts
from developer.planner import TOOL_TITLES
from developer.ttl import redis_ttl_seconds
from mcp.knowledge_base import KnowledgeBase
from mcp.registry import connect_external_servers
from mcp.tool_manager import MCPToolManager
from memory.conversation_memory import MemoryManager

load_dotenv()
logger = logging.getLogger(__name__)
_developer = None
TASK_INDEX_KEY = 'developer:tasks:running'
# A unique id per API process. Tasks record their owner so that a restarted or
# additional replica only repairs work whose worker is actually gone.
WORKER_ID = os.getenv('DEVTRACE_WORKER_ID', '').strip() or (
    f'{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}')
WORKER_KEY_PREFIX = 'developer:worker:'
WORKER_TTL_SECONDS = 90
WORKER_HEARTBEAT_SECONDS = 30
# Idle cadence of the in-process SSE keep-alive comment. A module constant so
# tests can shorten it instead of waiting 15 real seconds.
SSE_HEARTBEAT_SECONDS = 15.0


def _flag(name: str) -> bool:
    return os.getenv(name, '').strip().lower() in {'1', 'true', 'yes', 'enabled'}


def _stale_after_seconds() -> float:
    # The run timeout is 150s and the session lock is 180s; a shorter threshold
    # would let a query mark a healthy long-running task as interrupted.
    try:
        return max(240.0, float(os.getenv('DEVTRACE_TASK_STALE_SECONDS', '240')))
    except ValueError:
        return 240.0


def _task_key(task_id: str) -> str:
    return f'developer:task:{task_id}'


def _worker_key(worker_id: str) -> str:
    return WORKER_KEY_PREFIX + worker_id


async def register_worker(redis) -> None:
    """Publish worker liveness with a TTL so other replicas can tell it is alive."""
    await redis.setex(_worker_key(WORKER_ID), WORKER_TTL_SECONDS,
                      json.dumps({'pid': os.getpid(), 'started_at': time.time()}))


async def worker_heartbeat(redis) -> None:
    while True:
        try:
            await register_worker(redis)
        except Exception:
            logger.warning('worker 心跳写入失败', exc_info=True)
        await asyncio.sleep(WORKER_HEARTBEAT_SECONDS)


async def _worker_alive(redis, worker_id: str) -> bool:
    if not worker_id:
        return False
    return bool(await redis.get(_worker_key(worker_id)))


async def _should_repair(redis, record: dict) -> bool:
    """A running task is repairable when its owner is this worker or is gone."""
    if record.get('status') != 'running':
        return False
    owner = str(record.get('worker_id') or '')
    if not owner or owner == WORKER_ID:
        return True
    return not await _worker_alive(redis, owner)


async def _write_task_record(redis, task_id: str, user_id: str, status: str, run_id=None,
                             workspace: Optional[str] = None, trace_id: Optional[str] = None) -> dict:
    record = {'task_id': task_id, 'user_id': user_id, 'status': status, 'run_id': run_id,
              'worker_id': WORKER_ID, 'updated_at': time.time()}
    if workspace:
        record['workspace'] = workspace
    if trace_id:
        record['trace_id'] = trace_id
    await redis.setex(_task_key(task_id), redis_ttl_seconds(), json.dumps(record))
    # The index lets a restarted process find the tasks an earlier process left behind.
    if status == 'running':
        await redis.sadd(TASK_INDEX_KEY, task_id)
    else:
        await redis.srem(TASK_INDEX_KEY, task_id)
    return record


async def _touch_task(redis, task_id: str) -> None:
    """Refresh the heartbeat of a task that is still executing."""
    raw = await redis.get(_task_key(task_id))
    if not raw:
        return
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return
    if record.get('status') != 'running':
        return
    record['updated_at'] = time.time()
    await redis.setex(_task_key(task_id), redis_ttl_seconds(), json.dumps(record))


def task_is_stale(record: dict, now: Optional[float] = None) -> bool:
    if record.get('status') != 'running':
        return False
    updated = record.get('updated_at')
    if not isinstance(updated, (int, float)):
        return True
    return (time.time() if now is None else now) - updated > _stale_after_seconds()


async def _mark_interrupted(redis, task_id: str, record: dict) -> dict:
    # Re-read before writing so a concurrently finishing worker is not overwritten
    # with an interrupted status. Best effort: Redis still offers no transaction
    # across the read and the write, but the window is now tiny.
    raw = await redis.get(_task_key(task_id))
    try:
        current = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        current = None
    if (not isinstance(current, dict) or current.get('status') != 'running'
            or current.get('updated_at') != record.get('updated_at')):
        return current or record
    current['status'] = 'interrupted'
    current['interrupted_at'] = time.time()
    current['reason'] = '执行进程没有按时更新心跳；任务已标记为中断，可用同一会话发送“继续”重试'
    await redis.setex(_task_key(task_id), redis_ttl_seconds(), json.dumps(current))
    await redis.srem(TASK_INDEX_KEY, task_id)
    return current


async def reconcile_stale_tasks(redis) -> int:
    """Repair running tasks whose owning worker is gone.

    Safe with multiple replicas sharing Redis: a task owned by another live worker
    (its heartbeat key exists) is left alone, while tasks from this worker or from
    a worker whose TTL expired are marked interrupted.
    """
    members = getattr(redis, 'smembers', None)
    if members is None:
        return 0
    marked = 0
    for task_id in await members(TASK_INDEX_KEY):
        if isinstance(task_id, bytes):
            task_id = task_id.decode('utf-8')
        raw = await redis.get(_task_key(task_id))
        if not raw:
            await redis.srem(TASK_INDEX_KEY, task_id)
            continue
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            await redis.srem(TASK_INDEX_KEY, task_id)
            continue
        if record.get('status') == 'running':
            if await _should_repair(redis, record):
                await _mark_interrupted(redis, task_id, record)
                marked += 1
        else:
            await redis.srem(TASK_INDEX_KEY, task_id)
    return marked

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _developer
    app.state.jwt_secret, app.state.users, app.state.user_workspaces = auth_settings()
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
    mcp_client = None
    mcp_enabled = _flag('DEVTRACE_MCP_PUBLIC_TOOLS')
    mcp_all_tools = _flag('DEVTRACE_MCP_ALL_TOOLS')
    planning_candidates = _flag('DEVTRACE_PLANNING_CANDIDATES')
    sandbox_url = os.getenv("DEVELOPER_SANDBOX_URL", "http://developer-sandbox:8100")
    if mcp_enabled or mcp_all_tools:
        from mcp.client import InProcessTransport, MCPClient
        from mcp.runtime import MCPToolRuntime
        from mcp.server import build_default_server

        # The in-process server shares this process's knowledge bases and sandbox URL, so
        # the MCP path resolves the same records as the local tool path.
        runtime = MCPToolRuntime(knowledge=knowledge,
                                 public_knowledge=PublicDocsKnowledge(), sandbox_url=sandbox_url)
        mcp_client = MCPClient(InProcessTransport(build_default_server(runtime)))
        await mcp_client.initialize()
        logger.info('MCP tools enabled (public=%s all=%s server=%s)', mcp_enabled, mcp_all_tools,
                    mcp_client.server_info.get('serverInfo', {}))
    _developer = DeveloperAssistant(
        memory=memory, knowledge=knowledge, redis_client=memory._redis, model=model,
        tool_manager=MCPToolManager(api_key=key, base_url=base_url, model=model_name),
        sandbox_url=sandbox_url,
        public_knowledge=PublicDocsKnowledge(),
        mcp_client=mcp_client, mcp_public_tools=mcp_enabled, mcp_all_tools=mcp_all_tools,
        planning_candidates=planning_candidates,
    )
    heartbeat = None
    external = None
    # Connect after the assistant has registered its own tools, so the registry can see
    # every built-in name and skip a colliding external tool instead of replacing it.
    try:
        external = await connect_external_servers(
            _developer.tools, os.getenv('DEVTRACE_MCP_SERVERS', ''), reserved=list(TOOL_TITLES))
        app.state.external_mcp = external.summary()
        if app.state.external_mcp["servers"]:
            logger.info('外部 MCP 服务器：%s',
                        json.dumps(app.state.external_mcp, ensure_ascii=False)[:500])
        try:
            await register_worker(_developer.redis)
            heartbeat = asyncio.create_task(worker_heartbeat(_developer.redis))
            interrupted = await reconcile_stale_tasks(_developer.redis)
            if interrupted:
                logger.warning('启动时把 %s 个残留 running 任务标记为 interrupted', interrupted)
        except Exception:
            logger.warning('启动时任务状态修复失败', exc_info=True)
        try:
            yield
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if mcp_client is not None:
                await mcp_client.close()
    finally:
        # The registry owns the only subprocesses this process starts, so it is closed
        # even when a later startup step raises.
        if external is not None:
            await external.close()
        await memory.close()
        _developer = None

app = FastAPI(title="DevTrace 开发者平台 API 接入 Agent 助手", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware,
    allow_origins=[s.strip() for s in os.getenv('DEVTRACE_CORS_ORIGINS', 'http://localhost:5173').split(',') if s.strip()],
    allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'])

_REQUEST_ID_HEADER = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')


@app.middleware('http')
async def attach_request_id(request: Request, call_next):
    """Give every request an id, echo it back, and tag audit events raised while serving it."""
    supplied = request.headers.get('X-Request-ID', '')
    request_id = supplied if _REQUEST_ID_HEADER.fullmatch(supplied) else uuid.uuid4().hex
    audit.request_id_var.set(request_id)
    audit.remote_var.set(request.client.host if request.client else 'unknown')
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers['X-Request-ID'] = request_id
    return response


def _remote(request: Optional[Request]) -> str:
    return request.client.host if request is not None and request.client else 'unknown'


def _workspace_of(user: str) -> str:
    return workspace_for(getattr(app.state, 'user_workspaces', {}), user)


def _workspace_match(record: dict, workspace: str) -> bool:
    # Pre-tagging records carry no workspace field, so the owner still reads them from
    # any workspace; ownership is enforced separately. Audit reads report them as `default`.
    recorded = str(record.get('workspace') or '')
    return not recorded or recorded == workspace


async def _stamp_run(redis, run_id, workspace: str) -> None:
    """Attach the owning workspace to a run record written by the assistant."""
    if not run_id:
        return
    key = f'developer:run:{run_id}'
    raw = await redis.get(key)
    if not raw:
        return
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(record, dict):
        return
    record['workspace'] = workspace
    await redis.setex(key, redis_ttl_seconds(), json.dumps(record, ensure_ascii=False))

class DeveloperChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = Field(default=None, max_length=120)

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)

def current_user(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> str:
    return authenticated_user(request, credentials)

async def limit(key: str, maximum: int, *, retry_after: Optional[int] = None) -> None:
    # INCR and expiry are atomic, and shared by all application workers. Requests that
    # are rejected inside the window still increment it, so a burst of refusals extends
    # the effective throttle for that key. This is deliberate and stays unchanged.
    count = await _developer.redis.eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
        1, key)
    if count > maximum:
        headers = {'Retry-After': str(retry_after)} if retry_after else None
        raise HTTPException(429, '请求过于频繁，请稍后重试', headers=headers)

CHAT_LIMIT_MAXIMUM = 30
CHAT_LIMIT_WINDOW_SECONDS = 60
LOGIN_LIMIT_MAXIMUM = 10
LOGIN_LIMIT_RETRY_AFTER = 60
# Fail-open covers infrastructure faults only: a real quota breach raises HTTPException
# and always reaches the client. redis-py faults are RedisError/OSError subclasses, and
# asyncio timeouts surface as asyncio.TimeoutError.
LIMITER_INFRA_ERRORS = (RedisError, OSError, asyncio.TimeoutError)


def _login_limit_key(client: str) -> str:
    return 'developer:login-limit:' + hashlib.sha256(client.encode()).hexdigest()


async def enforce_limit(kind: str, key: str, maximum: int, *,
                        retry_after: Optional[int] = None) -> None:
    """Run one limiter window, failing open when Redis itself is unavailable.

    Availability wins over enforcement here: this is a demo-grade trade-off, so a Redis
    fault lets the request through with a warning and an audit event instead of returning
    500. Under a real outage the quota is simply not enforced; production deployments
    belong behind a gateway or replicated limiter instead of this path.
    """
    try:
        await limit(key, maximum, retry_after=retry_after)
    except HTTPException:
        raise
    except LIMITER_INFRA_ERRORS as exc:
        logger.warning('限流不可用，已 fail-open 放行: kind=%s reason=%s',
                       kind, type(exc).__name__)
        await audit.record(_developer.redis, f'{kind}.rate_limit_unavailable',
                           status='failure',
                           detail={'kind': kind, 'status': 'failure',
                                   'reason_code': 'limiter_unavailable'})


def _chat_limit_key(user_id: str) -> str:
    return 'developer:chat-limit:' + hashlib.sha256(user_id.encode()).hexdigest()


async def enforce_chat_limit(user_id: str) -> None:
    """Shared user quota: the sync and the streaming entry points spend the same key."""
    try:
        await enforce_limit('chat', _chat_limit_key(user_id), CHAT_LIMIT_MAXIMUM,
                            retry_after=CHAT_LIMIT_WINDOW_SECONDS)
    except HTTPException as exc:
        await audit.record(_developer.redis, 'chat.rate_limited', actor=user_id,
                           status='failure', workspace=_workspace_of(user_id),
                           detail={'kind': 'chat', 'status': 'failure',
                                   'reason_code': 'rate_limited'})
        raise exc from None

@app.post('/auth/login', tags=['认证'])
async def login(req: LoginRequest, request: Request):
    if _developer is None:
        raise HTTPException(503, '服务未就绪')
    client = request.client.host if request.client else 'unknown'
    await enforce_limit('login', _login_limit_key(client), LOGIN_LIMIT_MAXIMUM,
                        retry_after=LOGIN_LIMIT_RETRY_AFTER)
    # Perform the same password derivation for unknown users to reduce account enumeration.
    encoded = request.app.state.users.get(req.username, 'pbkdf2_sha256$' + '00' * 16 + '$' + '00' * 32)
    if not verify_password(req.password, encoded) or req.username not in request.app.state.users:
        await audit.record(_developer.redis, 'auth.login.failure', actor=req.username,
                           status='failure', remote=_remote(request),
                           workspace=_workspace_of(req.username))
        raise HTTPException(401, '用户名或密码错误')
    await audit.record(_developer.redis, 'auth.login.success', actor=req.username,
                       status='success', remote=_remote(request),
                       workspace=_workspace_of(req.username))
    return {'access_token': issue_token(req.username, request.app.state.jwt_secret), 'token_type': 'bearer', 'expires_in': 3600}

@app.get("/health")
async def health():
    if _developer is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "service": "devtrace"}


@app.get('/developer/prompts', tags=['开发者助手'])
async def developer_prompts(user_id: str = Depends(current_user)):
    """Prompt version and sizes only; never expose full system prompts."""
    await audit.record(_developer.redis, 'prompts.read', actor=user_id, status='success',
                       workspace=_workspace_of(user_id))
    return describe_prompts()

@app.post("/developer/chat", tags=["开发者助手"])
async def developer_chat(req: DeveloperChatRequest, user_id: str = Depends(current_user),
                         idempotency_key: Optional[str] = Header(default=None, alias='Idempotency-Key', max_length=120)):
    return await _chat_core(req, user_id, idempotency_key)


async def _chat_core(req: DeveloperChatRequest, user_id: str, idempotency_key: Optional[str],
                     *, enforce_limit: bool = True):
    # Internal-only flag: never expose it as a route parameter, or a client could set
    # it to false and skip the quota. The streaming route spends the key before the
    # response starts and calls here with enforce_limit=False.
    if _developer is None:
        raise HTTPException(503, "开发者助手未就绪")
    if enforce_limit:
        await enforce_chat_limit(user_id)
    import uuid
    workspace = _workspace_of(user_id)
    # Tool events raised deep inside the assistant run inherit the workspace from
    # this contextvar instead of threading it through the assistant signature.
    audit.workspace_var.set(workspace)
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
            started = time.perf_counter()
            result = await asyncio.wait_for(_developer.run(req.message, user_id, conversation_id), timeout=150)
        except asyncio.TimeoutError:
            await audit.record(_developer.redis, 'chat.sync', actor=user_id, status='failure',
                               workspace=workspace, detail={'reason_code': 'timeout'})
            raise HTTPException(504, '诊断超时，请使用同一会话重试')
        except Exception:
            await audit.record(_developer.redis, 'chat.sync', actor=user_id, status='failure',
                               workspace=workspace, detail={'reason_code': 'error'})
            raise
        if idem_key:
            await _developer.redis.setex(idem_key, redis_ttl_seconds(), json.dumps({'payload_hash': payload_hash, 'response': result}, ensure_ascii=False))
        await _stamp_run(_developer.redis, result.get('run_id'), workspace)
        await audit.record(
            _developer.redis, 'chat.sync', actor=user_id, resource=str(result.get('run_id') or ''),
            status='success', workspace=workspace,
            detail={'kind': str(result.get('kind') or ''),
                    'latency_ms': round((time.perf_counter() - started) * 1000, 1)})
        return result
    finally:
        # A disconnected SSE client cancels this task; shield the release so the
        # session lock is not leaked for the full 180s TTL.
        for lock in reversed(locks):
            try:
                await asyncio.shield(lock.release())
            except LockError:
                pass
            except BaseException:
                pass

@app.get('/developer/memory/{conversation_id}', tags=['开发者助手'])
async def developer_memory(conversation_id: str, user_id: str = Depends(current_user)):
    """Export this user's persisted memory for one conversation."""
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    result = await _developer.memory.export_memory(user_id, conversation_id)
    result['workspace'] = _workspace_of(user_id)
    await audit.record(_developer.redis, 'memory.export', actor=user_id,
                       resource=conversation_id, status='success',
                       workspace=result['workspace'])
    return result


@app.delete('/developer/memory/{conversation_id}', tags=['开发者助手'])
async def developer_memory_delete(conversation_id: str, user_id: str = Depends(current_user)):
    """Forget one conversation (working memory, summary, episodic records)."""
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    result = await _developer.memory.forget_conversation(user_id, conversation_id)
    plan_key = 'developer:plan:' + hashlib.sha256(
        f'{user_id}\0{conversation_id}'.encode()).hexdigest()
    deleted_plan = await _developer.redis.delete(plan_key)
    result['plan_deleted'] = bool(deleted_plan)
    result['workspace'] = _workspace_of(user_id)
    await audit.record(_developer.redis, 'memory.delete', actor=user_id,
                       resource=conversation_id, status='success',
                       workspace=result['workspace'])
    return result


@app.delete('/developer/profile', tags=['开发者助手'])
async def developer_profile_delete(user_id: str = Depends(current_user)):
    """Forget this user's long-term profile only (conversations and episodic memory stay)."""
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    workspace = _workspace_of(user_id)
    result = await _developer.memory.forget_profile(user_id)
    result['workspace'] = workspace
    await audit.record(_developer.redis, 'memory.profile_delete', actor=user_id,
                       resource='profile', status='success', workspace=workspace,
                       detail={'kind': 'profile', 'success': bool(result['profile_deleted'])})
    return result


@app.get('/developer/runs', tags=['开发者助手'])
async def developer_runs(user_id: str = Depends(current_user)):
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    key = 'developer:run-index:' + hashlib.sha256(user_id.encode()).hexdigest()
    workspace = _workspace_of(user_id)
    run_ids = await _developer.redis.zrevrange(key, 0, 49)
    items = []
    for run_id in run_ids:
        if isinstance(run_id, bytes):
            run_id = run_id.decode('utf-8')
        raw = await _developer.redis.get(f'developer:run:{run_id}')
        if not raw:
            continue
        record = json.loads(raw)
        if record.get('user_id') == user_id and _workspace_match(record, workspace):
            response = record['response']
            items.append({'run_id': run_id, 'conversation_id': response['conversation_id'],
                          'kind': response['kind'], 'status': response['plan']['status'],
                          'goal': response['plan']['goal'], 'workspace': workspace})
    return {'runs': items}


@app.get("/developer/runs/{run_id}", tags=["开发者助手"])
async def developer_run(run_id: str, user_id: str = Depends(current_user)):
    if _developer is None:
        raise HTTPException(503, "开发者助手未就绪")
    raw = await _developer.redis.get(f"developer:run:{run_id}")
    if not raw:
        raise HTTPException(404, "运行记录不存在或已过期")
    record = json.loads(raw)
    if record.get("user_id") != user_id or not _workspace_match(record, _workspace_of(user_id)):
        raise HTTPException(404, "运行记录不存在或已过期")
    return record["response"]


@app.get('/developer/tasks/{task_id}', tags=['开发者助手'])
async def developer_task(task_id: str, user_id: str = Depends(current_user)):
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    raw = await _developer.redis.get(f'developer:task:{task_id}')
    record = json.loads(raw) if raw else None
    if (not record or record.get('user_id') != user_id
            or not _workspace_match(record, _workspace_of(user_id))):
        raise HTTPException(404, '任务不存在或已过期')
    # Query-time repair: a task whose worker stopped cannot stay `running` forever,
    # but a task owned by another live replica must not be touched.
    if task_is_stale(record) and await _should_repair(_developer.redis, record):
        record = await _mark_interrupted(_developer.redis, task_id, record)
    # Lease bookkeeping is internal coordination, not task status.
    return {key: value for key, value in record.items()
            if key not in ('user_id', 'payload', 'lease_id', 'lease_until')}


@app.get('/developer/audit', tags=['开发者助手'])
async def developer_audit(limit: int = Query(50, ge=1, le=200),
                          user: str = Query('', max_length=120),
                          event: str = Query('', max_length=64),
                          workspace: str = Query('', max_length=40, pattern=r'^[A-Za-z0-9_-]*$'),
                          admin: str = Depends(require_admin)):
    """Newest-first audit events (admin only). Out-of-range limit is rejected with 422.

    The stream is append-only at the application layer; Redis itself is not WORM
    storage, so this is an operational log, not a tamper-proof record. Events
    written before workspace tagging existed are reported as `default`.
    """
    if _developer is None:
        raise HTTPException(503, '服务未就绪')
    events = await audit.read(_developer.redis, limit, user=user.strip(), event=event.strip(),
                              workspace=workspace.strip())
    await audit.record(_developer.redis, 'audit.read', actor=admin, status='success',
                       workspace=_workspace_of(admin), detail={'kind': 'audit.query'})
    return {'events': events, 'count': len(events)}


async def _count_worker_heartbeats(redis) -> int:
    """Best-effort count of live worker heartbeat keys (bounded SCAN)."""
    count = 0
    try:
        async for _key in redis.scan_iter(match=f'{WORKER_KEY_PREFIX}*', count=200):
            count += 1
    except Exception:  # noqa: BLE001 - metrics degrade gracefully if SCAN unsupported
        logger.warning('worker 计数 SCAN 失败', exc_info=True)
    return count


@app.get('/developer/dead-letter', tags=['运维'])
async def developer_dead_letter(limit: int = Query(50, ge=1, le=200),
                                admin: str = Depends(require_admin)):
    """Dead-letter task summaries (admin only): id, status, attempts, error, workspace."""
    if _developer is None:
        raise HTTPException(503, '服务未就绪')
    tasks = await task_queue.read_dead(_developer.redis, limit)
    await audit.record(_developer.redis, 'dead_letter.read', actor=admin, status='success',
                       workspace=_workspace_of(admin))
    return {'tasks': tasks, 'count': len(tasks)}


@app.post('/developer/tasks/{task_id}/replay', tags=['运维'])
async def developer_replay_task(task_id: str, admin: str = Depends(require_admin)):
    """Re-arm a dead-lettered task (admin only).

    Semantics: a task is replayable only when it is currently in the dead-letter
    list and its record is `failed`. Any other case - unknown id, missing record,
    not failed, or not in the dead list - returns 409 without revealing whether
    the task exists. On success the task is reset and pushed back to its queue.
    """
    if _developer is None:
        raise HTTPException(503, '服务未就绪')
    redis = _developer.redis
    result, record = await task_queue.replay(redis, task_id)
    workspace = str((record or {}).get('workspace') or _workspace_of(admin))
    trace_id = str((record or {}).get('trace_id') or '')
    if result != 'ok':
        await audit.record(redis, 'task.replayed', actor=admin, resource=task_id,
                           status='failure', workspace=workspace, trace_id=trace_id,
                           detail={'status': 'failure', 'reason_code': result})
        raise HTTPException(409, '任务不可重放（不在死信列表或状态不是失败）')
    await audit.record(redis, 'task.replayed', actor=admin, resource=task_id,
                       status='success', workspace=workspace, trace_id=trace_id,
                       detail={'status': 'queued', 'attempts': 0})
    logger.info('task replayed (trace_id=%s task_id=%s)', trace_id, task_id)
    return {'task_id': task_id, 'status': 'queued', 'replayed': True}


@app.get('/developer/metrics', tags=['运维'])
async def developer_metrics(admin: str = Depends(require_admin)):
    """Queue and worker snapshot (admin only). Bounded reads, no full SCAN of tasks."""
    if _developer is None:
        raise HTTPException(503, '服务未就绪')
    redis = _developer.redis
    metrics = await task_queue.queue_metrics(redis)
    metrics['workers'] = await _count_worker_heartbeats(redis)
    metrics['running'] = metrics['statuses'].get('running', 0)
    await audit.record(redis, 'metrics.read', actor=admin, status='success',
                       workspace=_workspace_of(admin))
    return metrics


def _queue_enabled() -> bool:
    return os.getenv('DEVTRACE_TASK_QUEUE', '').strip().lower() in {'1', 'true', 'yes', 'enabled', 'redis'}


async def _next_stream_event(get_task: 'asyncio.Task', timeout: float):
    """Await the next queued SSE event, or return None when the queue stayed idle.

    ``asyncio.wait_for`` cannot be used here: on Python 3.10 it returns the inner
    result whenever that future is already done, so an external ``Task.cancel()``
    landing in that window is silently discarded and the stream keeps running
    after the client is gone. ``asyncio.wait`` propagates the cancellation and
    leaves ``get_task`` alone on timeout, so no queued event is lost between two
    keep-alive ticks.
    """
    done, _pending = await asyncio.wait({get_task}, timeout=timeout)
    if not done:
        return None
    return get_task.result()


async def _queued_stream(req: 'DeveloperChatRequest', user_id: str,
                         idempotency_key: Optional[str]):
    """Enqueue a streamed diagnosis and relay worker events from Redis."""
    import uuid
    conversation_id = req.conversation_id or str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    workspace = _workspace_of(user_id)
    audit.workspace_var.set(workspace)
    audit.trace_id_var.set(trace_id)
    session_claimed = await task_queue.try_claim_session(
        _developer.redis, user_id, conversation_id, task_id)
    if not session_claimed:
        raise HTTPException(409, '该请求或会话正在处理中，请稍后重试')
    try:
        await task_queue.enqueue(_developer.redis, task_id, user_id,
                                 _redact(req.message), conversation_id, workspace=workspace,
                                 trace_id=trace_id)
    except Exception:
        # The task id is known here, so the rollback must only drop the lock this
        # request actually claimed.
        await task_queue.release_session_if_owner(
            _developer.redis, user_id, conversation_id, task_id)
        raise
    await audit.record(_developer.redis, 'task.enqueued', actor=user_id, resource=task_id,
                       status='queued', workspace=workspace, trace_id=trace_id,
                       detail={'status': 'queued', 'attempts': 0})
    logger.info('task enqueued (trace_id=%s task_id=%s priority=%s)',
                trace_id, task_id, task_queue.DEFAULT_PRIORITY)

    async def stream():
        import time
        terminal = False
        offset = 0
        last_beat = time.monotonic()
        try:
            yield f'event: started\ndata: {json.dumps({"task_id": task_id, "trace_id": trace_id})}\n\n'
            while True:
                for raw in await task_queue.read_events(_developer.redis, task_id, offset):
                    offset += 1
                    yield f'event: timeline\ndata: {raw}\n\n'
                record = await task_queue.get_task(_developer.redis, task_id) or {}
                status = record.get('status')
                if status in {'completed', 'waiting'}:
                    run_id = record.get('run_id')
                    raw = await _developer.redis.get(f'developer:run:{run_id}') if run_id else None
                    result = json.loads(raw).get('response') if raw else None
                    if result:
                        terminal = True
                        yield f'event: complete\ndata: {json.dumps(result, ensure_ascii=False)}\n\n'
                    else:
                        terminal = True
                        yield 'event: error\ndata: ' + json.dumps(
                            {'status': 500, 'detail': '任务已完成但结果不可用'}, ensure_ascii=False) + '\n\n'
                    break
                if status in {'failed', 'cancelled', 'interrupted'}:
                    terminal = True
                    detail = record.get('error') or '任务已取消或中断，请使用同一会话重试'
                    yield 'event: error\ndata: ' + json.dumps(
                        {'status': 500, 'detail': detail}, ensure_ascii=False) + '\n\n'
                    break
                now = time.monotonic()
                if now - last_beat >= 15:
                    last_beat = now
                    yield ': heartbeat\n\n'
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            await asyncio.shield(task_queue.request_cancel(_developer.redis, task_id))
            raise
        finally:
            if not terminal:
                try:
                    await asyncio.shield(task_queue.request_cancel(_developer.redis, task_id))
                except BaseException:
                    pass

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.post('/developer/chat/stream', tags=['开发者助手'])
async def developer_chat_stream(req: DeveloperChatRequest, user_id: str = Depends(current_user),
                                idempotency_key: Optional[str] = Header(default=None, alias='Idempotency-Key', max_length=120)):
    if _developer is None:
        raise HTTPException(503, '开发者助手未就绪')
    # Spend the shared user quota before any task record, queue entry or SSE bytes
    # exist, so a rejected request leaves no half-finished task behind.
    await enforce_chat_limit(user_id)
    if _queue_enabled():
        return await _queued_stream(req, user_id, idempotency_key)
    workspace = _workspace_of(user_id)

    async def stream():
        import uuid
        task_id = str(uuid.uuid4())
        trace_id = uuid.uuid4().hex
        terminal = False
        queue: asyncio.Queue = asyncio.Queue()
        # The generator is iterated in the response task, so tag the context here
        # as well for tool events emitted during the run.
        audit.workspace_var.set(workspace)
        audit.trace_id_var.set(trace_id)

        async def record_task(status, run_id=None):
            await _write_task_record(_developer.redis, task_id, user_id, status, run_id,
                                     workspace=workspace, trace_id=trace_id)

        async def audit_finish(status):
            await audit.record(_developer.redis, 'task.finished', actor=user_id, resource=task_id,
                               status=status, workspace=workspace, trace_id=trace_id,
                               detail={'status': status, 'attempts': 1})

        async def execute():
            nonlocal terminal
            reset_token = event_sink.set(lambda event: queue.put_nowait(('timeline', event)))
            try:
                result = await _chat_core(req, user_id, idempotency_key, enforce_limit=False)
                status = 'waiting' if result.get('plan', {}).get('status') == 'waiting' else 'completed'
                await record_task(status, result.get('run_id'))
                await audit_finish(status)
                terminal = True
                await queue.put(('complete', result))
            except asyncio.CancelledError:
                # Client disconnect cancels the worker task; writing the status
                # through shield keeps the record from staying `running`.
                try:
                    await asyncio.shield(record_task('cancelled'))
                    await asyncio.shield(audit_finish('cancelled'))
                except BaseException:
                    pass
                terminal = True
                raise
            except HTTPException as exc:
                logger.warning('流式诊断请求失败: %s %s', exc.status_code, exc.detail)
                await record_task('failed')
                await audit_finish('failed')
                terminal = True
                await queue.put(('error', {'status': exc.status_code, 'detail': exc.detail}))
            except Exception:
                logger.exception('流式诊断执行异常')
                await record_task('failed')
                await audit_finish('failed')
                terminal = True
                await queue.put(('error', {'status': 500, 'detail': '诊断执行失败，请稍后重试'}))
            finally:
                event_sink.reset(reset_token)
                try:
                    await queue.put(('end', {}))
                except BaseException:
                    pass

        await record_task('running')
        task = asyncio.create_task(execute())
        # One getter per event instead of a fresh `queue.get()` inside the wait: the
        # keep-alive timeout must not cancel a get that already dequeued an event.
        get_task = asyncio.ensure_future(queue.get())
        try:
            yield f'event: started\ndata: {json.dumps({"task_id": task_id, "trace_id": trace_id})}\n\n'
            while True:
                item = await _next_stream_event(get_task, SSE_HEARTBEAT_SECONDS)
                if item is None:
                    await _touch_task(_developer.redis, task_id)
                    yield ': heartbeat\n\n'
                    continue
                # Re-arm only after the event was taken, so an idle tick keeps the
                # pending get (and whatever it already dequeued) alive.
                get_task = asyncio.ensure_future(queue.get())
                kind, data = item
                if kind == 'end':
                    break
                # Every emitted event is activity too, so refresh the heartbeat here
                # as well as on the idle 15s tick.
                await _touch_task(_developer.redis, task_id)
                yield f'event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
        finally:
            async def settle():
                if not get_task.done():
                    get_task.cancel()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, get_task, return_exceptions=True)
                if not terminal:
                    await record_task('cancelled')
                    await audit_finish('cancelled')

            # Starlette cancels the response task when the client disconnects, so the
            # cleanup must run in its own shielded task or the status stays `running`.
            cleanup = asyncio.ensure_future(settle())
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                pass
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
