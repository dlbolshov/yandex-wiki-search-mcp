from mcp.client.session import ClientSession
from mcp.types import TextResourceContents
from pydantic import AnyUrl

from mcp_wiki.yfm import YFM_CHEATSHEET


class TestYfmCheatsheetResource:
    async def test_listed(self, client_session: ClientSession) -> None:
        result = await client_session.list_resources()
        uris = [str(resource.uri) for resource in result.resources]
        assert "wiki-mcp://yfm-cheatsheet" in uris

    async def test_read_returns_cheatsheet(
        self,
        client_session: ClientSession,
    ) -> None:
        result = await client_session.read_resource(AnyUrl("wiki-mcp://yfm-cheatsheet"))

        assert len(result.contents) > 0
        content = result.contents[0]
        assert isinstance(content, TextResourceContents)
        assert content.text == YFM_CHEATSHEET
        assert content.mimeType == "text/markdown"
