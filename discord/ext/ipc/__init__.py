import collections

from discord.ext.ipc.client import Client
from discord.ext.ipc.server import Server
from discord.ext.ipc.errors import *


_VersionInfo = collections.namedtuple(
    "_VersionInfo", 
    "major minor micro release serial"
)

__all__ = (
    "Client",
    "Server",
    "IPCError",
    "NoEndpointFoundError",
    "ServerConnectionRefusedError",
    "JSONEncodeError",
    "NotConnected",
)

version = "3.0.0"
version_info = _VersionInfo(3, 0, 0, "final", 0)
