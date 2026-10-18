"""DevTrace API: standalone developer-support service."""
import asyncio
import hashlib
import json
import logging
import os
import socket
import time
import uuid
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
from developer import task_queue
from developer.assistant import AnthropicDecisionModel, DeveloperAssistant, RuleDecisionModel, _redact
from developer.data import DOCS
from developer.events import event_sink
from developer.public_knowledge import PublicDocsKnowledge
from developer.prompts import describe as describe_prompts
from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager
from memory.conversation_memory import MemoryManager

load_dotenv()
logger = logging.getLogger(__name__)
_developer = None
TASK_TTL_SECONDS = 86400
TASK_INDEX_KEY = 'developer:tasks:running'
# A unique id per API process. Tasks record their owner so that a restarted or
# additional replica only repairs work whose worker is actually gone.
WORKER_ID = os.getenv('DEVTRACE_WORKER_ID', '').strip() or (
    f'{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}')
WORKER_KEY_PREFIX = 'developer:worker:'
WORKER_TTL_SECONDS = 90
WORKER_HEARTBEAT_SECONDS = 30


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


async def _write_task_record(redis, task_id: str, user_id: str, status: str, run_id=None) -> dict:
    record = {'task_id': task_id, 'user_id': user_id, 'status': status, 'run_id': run_id,
              'worker_id': WORKER_ID, 'updated_at': time.time()}
    await redis.setex(_task_key(task_id), TASK_TTL_SECONDS, json.dumps(record))
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
    await redis.setex(_task_key(task_id), TASK_TTL_SECONDS, json.dumps(record))


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
    await redis.setex(_task_key(task_id), TASK_TTL_SECONDS, json.dumps(current))
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
    mcp_client = None
    mcp_enabled = os.getenv('DEVTRACE_MCP_PUBLIC_TOOLS', '').strip().lower() in {
        '1', 'true', 'yes', 'enabled'}
    if mcp_enabled:
        from mcp.client import InProcessTransport, MCPClient
        from mcp.server import build_default_server

        mcp_client = MCPClient(InProcessTransport(build_default_server()))
        await mcp_client.initialize()
        logger.info('MCP public tools enabled (server=%s)', mcp_client.server_info.get('serverInfo', {}))
    _developer = DeveloperAssistant(
        memory=memory, knowledge=knowledge, redis_client=memory._redis, model=model,
        tool_manager=MCPToolManager(api_key=key, base_url=base_url, model=model_name),
        sandbox_url=os.getenv("DEVELOPER_SANDBOX_URL", "http://developer-sandbox:8100"),
        public_knowledge=PublicDocsKnowledge(),
        mcp_client=mcp_client, mcp_public_tools=mcp_enabled,
    )
    heartbeat = None
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


@app.get('/developer/prompts', tags=['开发者助手'])
async def developer_prompts(user_id: str = Depends(current_user)):
    """Prompt version and sizes only; never expose full system prompts."""
    return describe_prompts()

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
        # A disconnected SSE client cancels this task; shield the release so the
        # session lock is not leaked for the full 180s TTL.
        for lock in reversed(locks):
            try:
                await asyncio.shield(lock.release())
            except LockError:
                pass
            except BaseException:
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
    # Query-time repair: a task whose worker stopped cannot stay `running` forever,
    # but a task owned by another live replica must not be touched.
    if task_is_stale(record) and await _should_repair(_developer.redis, record):
        record = await _mark_interrupted(_developer.redis, task_id, record)
    return {key: value for key, value in record.items() if key not in ('user_id', 'payload')}


def _queue_enabled() -> bool:
    return os.getenv('DEVTRACE_TASK_QUEUE', '').strip().lower() in {'1', 'true', 'yes', 'enabled', 'redis'}


async def _queued_stream(req: 'DeveloperChatRequest', user_id: str,
                         idempotency_key: Optional[str]):
    """Enqueue a streamed diagnosis and relay worker events from Redis."""
    import uuid
    conversation_id = req.conversation_id or str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    session_claimed = await task_queue.try_claim_session(
        _developer.redis, user_id, conversation_id, task_id)
    if not session_claimed:
        raise HTTPException(409, '该请求或会话正在处理中，请稍后重试')
    try:
        await task_queue.enqueue(_developer.redis, task_id, user_id,
                                 _redact(req.message), conversation_id)
    except Exception:
        await task_queue.release_session(_developer.redis, user_id, conversation_id)
        raise

    async def stream():
        import time
        terminal = False
        offset = 0
        last_beat = time.monotonic()
        try:
            yield f'event: started\ndata: {json.dumps({"task_id": task_id})}\n\n'
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
    if _queue_enabled():
        return await _queued_stream(req, user_id, idempotency_key)
    async def stream():
        import uuid
        task_id = str(uuid.uuid4())
        terminal = False
        queue: asyncio.Queue = asyncio.Queue()

        async def record_task(status, run_id=None):
            await _write_task_record(_developer.redis, task_id, user_id, status, run_id)

        async def execute():
            nonlocal terminal
            reset_token = event_sink.set(lambda event: queue.put_nowait(('timeline', event)))
            try:
                result = await developer_chat(req, user_id, idempotency_key)
                await record_task('waiting' if result.get('plan', {}).get('status') == 'waiting' else 'completed', result.get('run_id'))
                terminal = True
                await queue.put(('complete', result))
            except asyncio.CancelledError:
                # Client disconnect cancels the worker task; writing the status
                # through shield keeps the record from staying `running`.
                try:
                    await asyncio.shield(record_task('cancelled'))
                except BaseException:
                    pass
                terminal = True
                raise
            except HTTPException as exc:
                logger.warning('流式诊断请求失败: %s %s', exc.status_code, exc.detail)
                await record_task('failed')
                terminal = True
                await queue.put(('error', {'status': exc.status_code, 'detail': exc.detail}))
            except Exception:
                logger.exception('流式诊断执行异常')
                await record_task('failed')
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
        try:
            yield f'event: started\ndata: {json.dumps({"task_id": task_id})}\n\n'
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    await _touch_task(_developer.redis, task_id)
                    yield ': heartbeat\n\n'
                    continue
                if kind == 'end':
                    break
                # Every emitted event is activity too, so refresh the heartbeat here
                # as well as on the idle 15s tick.
                await _touch_task(_developer.redis, task_id)
                yield f'event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'
        finally:
            async def settle():
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if not terminal:
                    await record_task('cancelled')

            # Starlette cancels the response task when the client disconnects, so the
            # cleanup must run in its own shielded task or the status stays `running`.
            cleanup = asyncio.ensure_future(settle())
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                pass
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
