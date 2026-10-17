"""stdio entrypoint:  python -m chainmind.mcp_server

Point any MCP client at this. Example Claude Desktop config:

    {
      "mcpServers": {
        "chainmind": {
          "command": "/abs/path/.venv/bin/python",
          "args": ["-m", "chainmind.mcp_server"],
          "cwd": "/abs/path/AgentWar",
          "env": {"GEMINI_API_KEY": "...", "GOOGLE_MAPS_API_KEY": "..."}
        }
      }
    }
"""
from chainmind.mcp_server.server import build_server

if __name__ == "__main__":
    build_server().run(transport="stdio")
