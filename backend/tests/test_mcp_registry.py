"""External MCP server registry: default-deny read tools over stdio only.

Checklist coverage for TASK-0004 #16/#17: naming, the read-only whitelist, built-in
name conflicts, an unavailable server, an empty or invalid configuration starting no
process, and server-held parameters never being forwarded.
"""
import asyncio
import json
import os
import sys
import unittest

from mcp.registry import (SERVER_HELD_PARAMS, ExternalMCPServers, connect_external_servers,
                          parse_servers_config, sanitize_schema, tool_hints, tool_name)
from mcp.tool_manager import MCPToolManager, Tool

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(BACKEND_DIR, "tests", "fake_ext_mcp_server.py")
BUILT_IN_TOOLS = ["search_api_docs", "get_request_trace", "inspect_key_scope",
                  "get_webhook_delivery", "create_support_ticket"]


def fixture_config(name="fixture"):
    return json.dumps([{"name": name, "command": sys.executable, "args": [FIXTURE]}])


def noop_tool(name: str) -> Tool:
    async def handler(_params, _context):
        return {"ok": True}
    return Tool(name=name, description=name, handler=handler,
                schema={"type": "object", "properties": {"query": {"type": "string"}},
                        "required": [], "additionalProperties": False})


class ConfigParsingTests(unittest.TestCase):
    def test_empty_and_blank_config_start_no_server(self):
        self.assertEqual(parse_servers_config(""), [])
        self.assertEqual(parse_servers_config("   "), [])

    def test_invalid_json_is_dropped_rather_than_raising(self):
        self.assertEqual(parse_servers_config("{not json"), [])
        self.assertEqual(parse_servers_config('{"name": "x"}'), [])

    def test_entries_must_be_objects_with_only_supported_fields(self):
        self.assertEqual(parse_servers_config('["python", "server.py"]'), [])
        # An HTTP/SSE style entry carries a field the stdio registry does not know.
        self.assertEqual(parse_servers_config(
            '[{"name": "ok", "command": "python", "url": "http://x"}]'), [])

    def test_transport_is_stdio_only(self):
        entries = parse_servers_config('[{"name": "ok", "command": "python", "args": ["s.py"]}]')
        self.assertEqual(entries, [{"name": "ok", "command": "python", "args": ["s.py"]}])
        self.assertEqual(parse_servers_config('[{"name": "ok", "args": ["s.py"]}]'), [])

    def test_server_names_are_restricted(self):
        for bad in ("Bad Name", "a/b", "", "x" * 40):
            with self.subTest(name=bad):
                self.assertEqual(parse_servers_config(
                    json.dumps([{"name": bad, "command": "python", "args": []}])), [])

    def test_arguments_are_stringified_and_bounded(self):
        entries = parse_servers_config(json.dumps(
            [{"name": "svc", "command": "python", "args": ["a", 1, True,
                                                          *[str(i) for i in range(30)]]}]))
        self.assertEqual(len(entries[0]["args"]), 20)
        self.assertEqual(entries[0]["args"][:3], ["a", "1", "True"])
        self.assertTrue(all(isinstance(arg, str) for arg in entries[0]["args"]))


class ClassificationTests(unittest.TestCase):
    def test_read_hints_are_accepted(self):
        for name, description in (("search_docs", "Search the catalogue"),
                                  ("list_items", "Browse items"),
                                  ("get_status", "Report the current status")):
            with self.subTest(name=name):
                self.assertEqual(tool_hints(name, description, {})[0], "read")

    def test_write_hints_are_rejected_even_with_a_read_verb(self):
        for name, description in (("create_ticket", "Create a support ticket"),
                                  ("search_and_send", "Search then send a message"),
                                  ("delete_item", "Delete one item")):
            with self.subTest(name=name):
                self.assertEqual(tool_hints(name, description, {})[0], "write")

    def test_unclassifiable_tools_default_to_denied(self):
        self.assertEqual(tool_hints("frobnicate_gizmo", "Frobnicates the gizmo", {})[0], "unknown")

    def test_write_hint_in_a_parameter_name_marks_the_tool_write(self):
        schema = {"type": "object", "properties": {"delete_after_read": {"type": "boolean"}}}
        self.assertEqual(tool_hints("fetch_report", "Fetch a report", schema)[0], "write")

    def test_server_held_params_are_named(self):
        self.assertEqual(set(SERVER_HELD_PARAMS),
                         {"user_id", "conversation_id", "idempotency_key", "ticket_id", "api_key"})

    def test_sanitize_schema_drops_server_held_parameters(self):
        cleaned = sanitize_schema({"type": "object", "properties": {
            "query": {"type": "string"}, "user_id": {"type": "string"},
            "api_key": {"type": "string"}}, "required": ["query", "user_id"]})
        self.assertEqual(list(cleaned["properties"]), ["query"])
        self.assertEqual(cleaned["required"], ["query"])
        self.assertFalse(cleaned["additionalProperties"])

    def test_sanitize_schema_survives_a_missing_or_bad_schema(self):
        self.assertEqual(sanitize_schema({})["properties"], {})
        self.assertEqual(sanitize_schema("not-a-schema")["properties"], {})

    def test_tool_name_is_prefixed_with_the_server(self):
        self.assertEqual(tool_name("svc", "search_docs"), "ext_svc_search_docs")


class RegistryConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = MCPToolManager(api_key="test-key")
        for name in BUILT_IN_TOOLS:
            self.manager.register(noop_tool(name))

    async def connect(self, raw_config, reserved=()):
        servers = ExternalMCPServers()
        self.addAsyncCleanup(servers.close)
        await servers.connect(self.manager, raw_config, reserved=list(reserved), timeout_s=20)
        return servers

    async def test_empty_config_starts_no_process_and_registers_nothing(self):
        servers = await self.connect("")
        self.assertEqual(servers.clients, [])
        self.assertEqual(servers.registered, [])
        self.assertEqual(servers.summary()["servers"], [])
        self.assertEqual(sorted(self.manager.tool_names), sorted(BUILT_IN_TOOLS))

    async def test_invalid_config_starts_no_process(self):
        servers = await self.connect("{broken")
        self.assertEqual(servers.clients, [])
        self.assertEqual(sorted(self.manager.tool_names), sorted(BUILT_IN_TOOLS))

    async def test_helper_skips_connecting_when_config_is_empty(self):
        servers = await connect_external_servers(self.manager, "")
        self.assertEqual(servers.clients, [])

    async def test_registration_naming_and_read_only_filtering(self):
        servers = await self.connect(fixture_config())
        self.assertEqual(servers.registered,
                         ["ext_fixture_search_widgets", "ext_fixture_search_api_docs"])
        reasons = {item["tool"]: item["reason"] for item in servers.skipped}
        self.assertEqual(reasons["ext_fixture_create_widget"], "not_read_only:write")
        self.assertEqual(reasons["ext_fixture_migrate_ledger"], "not_read_only:unknown")
        self.assertEqual(reasons["ext_fixture_fetch_ticket_id"], "not_read_only:write")
        summary = servers.summary()
        self.assertEqual(summary["servers"][0]["discovered"], 5)
        self.assertEqual(summary["servers"][0]["registered"], 2)

    async def test_builtin_name_conflict_is_skipped(self):
        servers = await self.connect(fixture_config(), reserved=["ext_fixture_search_widgets"])
        self.assertNotIn("ext_fixture_search_widgets", servers.registered)
        conflicts = [item for item in servers.skipped if item["reason"] == "name_conflict"]
        self.assertEqual([item["tool"] for item in conflicts], ["ext_fixture_search_widgets"])
        # The built-in registration is left untouched.
        self.assertIn("search_api_docs", self.manager.tool_names)

    async def test_same_name_twice_conflicts_through_the_manager(self):
        servers = await self.connect(json.dumps([
            {"name": "twin", "command": sys.executable, "args": [FIXTURE]},
            {"name": "twin", "command": sys.executable, "args": [FIXTURE]}]))
        self.assertEqual(servers.registered,
                         ["ext_twin_search_widgets", "ext_twin_search_api_docs"])
        conflicts = [item["tool"] for item in servers.skipped if item["reason"] == "name_conflict"]
        self.assertEqual(sorted(conflicts),
                         ["ext_twin_search_api_docs", "ext_twin_search_widgets"])

    async def test_unavailable_server_is_skipped_without_touching_builtins(self):
        config = json.dumps([
            {"name": "ghost", "command": "definitely-not-installed", "args": []},
            {"name": "fixture", "command": sys.executable, "args": [FIXTURE]}])
        servers = await self.connect(config)
        ghost = [item for item in servers.skipped if item["server"] == "ghost"]
        self.assertTrue(ghost)
        self.assertTrue(ghost[0]["reason"].startswith("unavailable"))
        self.assertEqual([entry["name"] for entry in servers.summary()["servers"]], ["fixture"])
        self.assertEqual(servers.registered,
                         ["ext_fixture_search_widgets", "ext_fixture_search_api_docs"])
        self.assertIn("search_api_docs", self.manager.tool_names)

    async def test_server_held_parameters_are_not_overridable(self):
        await self.connect(fixture_config())
        result = await self.manager.call(
            "ext_fixture_search_widgets",
            {"query": "bolt", "user_id": "victim", "api_key": "secret",
             "conversation_id": "c", "idempotency_key": "k", "ticket_id": "t"})
        self.assertTrue(result.success, result.error)
        # The fixture echoes the argument names it received; only the read field arrives.
        self.assertEqual(result.data["received"], ["query"])
        self.assertEqual(result.data["widgets"][0]["id"], "bolt")

    async def test_tool_metadata_never_asks_for_server_held_parameters(self):
        await self.connect(fixture_config())
        schema = self.manager._tools["ext_fixture_search_widgets"].schema
        self.assertNotIn("user_id", schema["properties"])
        self.assertNotIn("api_key", schema["properties"])
        self.assertEqual(schema["required"], ["query"])

    async def test_close_terminates_the_child_process(self):
        servers = await self.connect(fixture_config())
        process = servers.clients[0][1].transport._process
        self.assertIsNotNone(process)
        await servers.close()
        await asyncio.wait_for(process.wait(), timeout=30)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(servers.clients, [])

    async def test_registered_tool_keeps_the_server_description_and_skips_cache(self):
        await self.connect(fixture_config())
        tool = self.manager._tools["ext_fixture_search_widgets"]
        self.assertEqual(tool.description, "Search the widget catalogue")
        self.assertEqual(tool.cache_ttl, 0)


if __name__ == "__main__":
    unittest.main()
