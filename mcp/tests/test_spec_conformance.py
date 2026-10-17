"""What the MCP spec and clients expect from every tool, prompt and resource this server lists."""

from __future__ import annotations

import pytest
from conftest import TEST_ENV

from agent_obs_mcp import __version__
from agent_obs_mcp.config import load_settings
from agent_obs_mcp.server import build_server

pytestmark = pytest.mark.anyio

WRITE_TOOLS = {"create_alert", "create_dashboard"}


def server():
    return build_server(load_settings(TEST_ENV))


async def test_every_tool_parameter_is_described() -> None:
    missing = [
        f"{tool.name}.{name}"
        for tool in await server().list_tools()
        for name, schema in (tool.inputSchema.get("properties") or {}).items()
        if not schema.get("description")
    ]
    assert missing == []


async def test_tool_annotations_match_what_each_tool_does() -> None:
    for tool in await server().list_tools():
        hints = tool.annotations
        assert hints is not None, tool.name
        assert hints.openWorldHint is False, tool.name
        assert hints.readOnlyHint is (tool.name not in WRITE_TOOLS), tool.name
        assert hints.destructiveHint is False, tool.name


async def test_prompts_have_titles_and_described_arguments() -> None:
    for prompt in await server().list_prompts():
        assert prompt.title and prompt.description, prompt.name
        for argument in prompt.arguments or []:
            assert argument.description, f"{prompt.name}.{argument.name}"


async def test_resources_have_titles_and_types() -> None:
    for resource in await server().list_resources():
        assert resource.title and resource.description and resource.mimeType, resource.uri


def test_server_reports_its_own_version_not_the_sdk_version() -> None:
    options = server()._mcp_server.create_initialization_options()
    assert options.server_version == __version__


async def test_every_prompt_works_without_arguments() -> None:
    mcp = server()
    for prompt in await mcp.list_prompts():
        result = await mcp.get_prompt(prompt.name, {})
        assert result.messages, prompt.name
