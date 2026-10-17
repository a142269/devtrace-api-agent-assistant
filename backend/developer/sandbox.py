"""Fictional developer platform; run with uvicorn developer.sandbox:app --port 8100."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from developer.data import KEYS, REQUESTS, WEBHOOK_CASES

app = FastAPI(title="DevTrace developer sandbox", version="1.0")
DB_PATH = Path(os.getenv("DEVELOPER_SANDBOX_DB", "data/developer_sandbox.sqlite"))


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        issue_id TEXT NOT NULL,
        summary TEXT NOT NULL
    )""")
    return connection


class TicketInput(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=160)
    user_id: str = Field(min_length=1, max_length=120)
    conversation_id: str = Field(min_length=1, max_length=120)
    issue_id: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)


@app.get("/health")
def health():
    return {"status": "ok", "mock": True}


@app.get("/mock/requests/{request_id}")
def get_request(request_id: str):
    if request_id not in REQUESTS:
        raise HTTPException(404, "模拟平台无此 request_id")
    return {**REQUESTS[request_id], "mock": True}


@app.get("/mock/keys/{key_prefix}")
def get_key(key_prefix: str):
    if key_prefix not in KEYS:
        raise HTTPException(404, "模拟平台无此 key_prefix")
    return {**KEYS[key_prefix], "mock": True}


@app.get("/mock/webhooks/{delivery_id}")
def get_webhook(delivery_id: str):
    if delivery_id not in WEBHOOK_CASES:
        raise HTTPException(404, "模拟平台无此 delivery_id")
    return {**WEBHOOK_CASES[delivery_id], "mock": True}


@app.post("/mock/tickets")
def create_ticket(body: TicketInput):
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO tickets (idempotency_key,user_id,conversation_id,issue_id,summary) VALUES (?,?,?,?,?)",
                (body.idempotency_key, body.user_id, body.conversation_id, body.issue_id, body.summary),
            )
            row = connection.execute("SELECT id,issue_id FROM tickets WHERE idempotency_key=?", (body.idempotency_key,)).fetchone()
    return {"ticket_id": f"MOCK-{row['id']:05d}", "issue_id": row["issue_id"], "mock": True}
