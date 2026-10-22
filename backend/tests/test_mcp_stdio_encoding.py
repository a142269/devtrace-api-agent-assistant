"""MCP stdio is UTF-8 end to end, without asking the caller to set PYTHONUTF8.

P10 found that the bundled stdio server inherited the Windows locale codec (cp936), so a
UTF-8 client crashed decoding the Chinese tool text unless the whole suite was launched
with ``PYTHONUTF8=1``. These cases pin the fix on both sides of the pipe.
"""
import asyncio
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

from developer.planner import TOOL_TITLES
from mcp import server as mcp_server
from mcp.client import StdioTransport
from mcp.server import force_utf8_stdio

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_READ_TOOLS = {"search_api_docs", "search_quota_docs", "get_request_trace",
                     "get_related_request_trace", "inspect_key_scope",
                     "get_webhook_delivery", "search_public_docs", "lookup_error_playbook"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "clientInfo": {"name": "test", "version": "1"}}}


def _payload(methods: list) -> bytes:
    lines = [INITIALIZE, {"jsonrpc": "2.0", "method": "notifications/initialized"}]
    for index, method in enumerate(methods, start=2):
        lines.append({"jsonrpc": "2.0", "id": index, "method": method, "params": {}})
    return "".join(json.dumps(item) + "\n" for item in lines).encode()


class StdioChildEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_list_survives_a_locale_codec_child(self):
        """The child is spawned with PYTHONUTF8 absent: the server must still speak UTF-8."""
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp.server", cwd=BACKEND_DIR, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(_payload(["tools/list"])), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            self.fail("the stdio MCP entrypoint did not finish")
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace")[:400])
        text = stdout.decode("utf-8")        # strict: a cp936 payload would raise here
        self.assertNotIn("\ufffd", text)
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        by_id = {item.get("id"): item for item in lines}
        tools = by_id[2]["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, SERVER_READ_TOOLS)
        self.assertEqual({tool["name"]: tool["description"] for tool in tools},
                         {name: TOOL_TITLES[name] for name in SERVER_READ_TOOLS})
        self.assertEqual(env.get("PYTHONUTF8"), None,
                         "the case is meaningless if the child still inherits UTF-8 mode")


class LocaleCodecControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_harness_can_still_see_a_locale_codec_child(self):
        """Negative control: a child that does NOT reconfigure writes GBK here.

        Without this the UTF-8 case could go silently green on a host whose default
        codec already is UTF-8, which would stop testing anything.
        """
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONUTF8', 'PYTHONIOENCODING')}
        script = "sys.stdout.write('检索对应版本的 API 文档'); sys.stdout.flush()"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", f"import sys; {script}", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        if os.name == "nt":
            self.assertEqual(stdout.decode("gbk"), "检索对应版本的 API 文档")
            with self.assertRaises(UnicodeDecodeError):
                stdout.decode("utf-8")
        else:
            self.assertEqual(stdout.decode("utf-8"), "检索对应版本的 API 文档")


class ForceUtf8HelperTests(unittest.TestCase):
    def test_reconfigures_a_locale_encoded_text_stream(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="cp936")
        self.assertEqual(force_utf8_stdio(stream), ["TextIOWrapper"])
        self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")

    def test_skips_streams_without_reconfigure(self):
        class Legacy:
            def write(self, text):
                pass

        self.assertEqual(force_utf8_stdio(Legacy(), None), [])

    def test_a_stream_that_refuses_reconfigure_is_skipped_not_fatal(self):
        class Refusing:
            def reconfigure(self, **_kwargs):
                raise ValueError("stand-alone newline mode")

        self.assertEqual(force_utf8_stdio(Refusing()), [])


class StdioLoopStreamSwapTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_answers_through_streams_that_cannot_reconfigure(self):
        class CapturingStdout:
            def __init__(self):
                self.lines = []

            def flush(self):
                pass

            def write(self, text):
                self.lines.append(text)

        stdout = CapturingStdout()
        stdin = io.TextIOWrapper(io.BytesIO(_payload(["tools/list"])), encoding="cp936")
        with patch.object(mcp_server.sys, "stdin", stdin), \
                patch.object(mcp_server.sys, "stdout", stdout):
            await asyncio.wait_for(mcp_server._stdio_loop(), timeout=60)
        response = json.loads(stdout.lines[-1])
        self.assertEqual(response["id"], 2)
        self.assertEqual({tool["name"] for tool in response["result"]["tools"]},
                         SERVER_READ_TOOLS)


class StdioTransportEnvTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_environment_forces_utf8(self):
        started = {}

        class FakeProcess:
            stdin = None
            stdout = None

        async def fake_exec(*command, **kwargs):
            started["command"] = command
            started.update(kwargs)
            return FakeProcess()

        with patch.dict(os.environ, {"PYTHONUTF8": "0", "PATH": os.environ.get("PATH", "")}), \
                patch("asyncio.create_subprocess_exec", fake_exec):
            transport = StdioTransport()
            await transport.start()
        self.assertEqual(started["env"]["PYTHONUTF8"], "1")
        self.assertEqual(started["env"]["PATH"], os.environ.get("PATH", ""))
        self.assertEqual(started["command"], (sys.executable, "-m", "mcp.server"))

    async def test_third_party_command_keeps_the_forced_encoding(self):
        transport = StdioTransport(command=sys.executable, args=["-c", "pass"])
        with patch.dict(os.environ, {"PYTHONUTF8": "0"}):
            env = transport._child_env()
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(transport.command, [sys.executable, "-c", "pass"])


if __name__ == "__main__":
    unittest.main()
