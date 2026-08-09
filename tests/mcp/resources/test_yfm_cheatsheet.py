from mcp import Client
from mcp.types import TextResourceContents

from mcp_wiki.yfm import YFM_CHEATSHEET


class TestYfmCheatsheetResource:
    async def test_listed(self, client: Client) -> None:
        result = await client.list_resources()
        uris = [str(resource.uri) for resource in result.resources]
        assert "wiki-mcp://yfm-cheatsheet" in uris

    async def test_read_returns_cheatsheet(
        self,
        client: Client,
    ) -> None:
        result = await client.read_resource("wiki-mcp://yfm-cheatsheet")

        assert len(result.contents) > 0
        content = result.contents[0]
        assert isinstance(content, TextResourceContents)
        assert content.text == YFM_CHEATSHEET
        assert content.mime_type == "text/markdown"
