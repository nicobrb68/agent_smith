import os
import sys
from typing import Dict, Any
from mcp.server.fastmcp import FastMCP

# On importe ta Sandbox et sa config
from student.sandbox import Sandbox
from student.sandbox_config import SandboxConfig

# On initialise le serveur FastMCP
mcp = FastMCP("MBPP Sandbox Server")