from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, TYPE_CHECKING, TypeVar

import uvloop  # type: ignore
import uvicorn
import asyncio
from unsync import unsync

from discord.ext.ipc.errors import *
from redbot.core.bot import Red
from redbot.core import commands

from fastapi import FastAPI, WebSocket

if TYPE_CHECKING:
    from typing_extensions import ParamSpec, TypeAlias

    P = ParamSpec("P")
    T = TypeVar("T")

    RouteFunc: TypeAlias = Callable[P, T]


log = logging.getLogger(__name__)


def route(name: Optional[str] = None) -> Callable:
    """
    Used to register a coroutine as an endpoint when you don't have
    access to an instance of :class:`.Server`

    Parameters
    ----------
    name: str
        The endpoint name. If not provided the method name will be
        used.
    """

    def decorator(func: Any):
        if not name:
            Server.ROUTES[func.__name__] = func
        else:
            Server.ROUTES[name] = func

        return func

    return decorator


class IpcServerResponse:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._json: Dict[str, Any] = data
        self.length: int = len(data)

        self.endpoint: Any = data["endpoint"]

        self.__refresh()

    def __refresh(self) -> None:
        for key, value in self._json['data'].items():
            setattr(self, key, value)

    def __getitem__(self, key: Any) -> Any:
        data = self._json.__getitem__(key)
        self.__refresh()
        return data

    def __contains__(self, k: Any) -> bool:
        return self._json.__contains__(k)

    def __delitem__(self, k: Any):
        self._json.__delitem__(k)
        self.__refresh()

    def __iter__(self) -> Iterable[Tuple[str, Any]]:
        yield from self._json["data"].items()

    def get(self, k: Any, default: Any = None) -> Any:
        return self._json.get(k, default)

    def pop(self, k: Any, default: Any = None) -> Any:
        data = self._json.pop(k, default)
        self.__refresh()
        return data

    def __setitem__(self, k: Any, v: Any) -> None:
        self._json(k, v)  # type: ignore
        self.__refresh()

    def to_json(self) -> Dict[str, Any]:
        return self._json

    def __repr__(self) -> str:
        return (
            f"<IpcServerResponse length={self.length} {' '.join(f'{k}={v}' for k, v in self._json['data'].items())} "
            f"endpoint={self.to_json()['endpoint']}>"
        )

    def __str__(self) -> str:
        return self.__repr__()


class Server:
    """The IPC server. Usually used on the bot process for receiving
    requests from the client.

    Attributes
    ----------
    bot: :class:`~redbot.core.bot.Red`
        Your bot instance
    host: str
        The host to run the IPC Server on. Defaults to localhost.
    port: int
        The port to run the IPC Server on. Defaults to 8765.
    secret_key: str
        A secret key. Used for authentication and should be the same as
        your client's secret key.
    do_multicast: bool
        Turn multicasting on/off. Defaults to True
    multicast_port: int
        The port to run the multicasting server on. Defaults to 20000
    """

    ROUTES: Dict[Any, Any] = {}

    def __init__(
        self,
        *,
        bot: Red,
        host: str = "localhost",
        port: int = 8765,
        secret_key: Optional[str] = None,
        do_multicast: bool = True,
        multicast_port: int = 20000,
    ):
        self.bot: Red = bot
        self.loop: asyncio.AbstractEventLoop = bot.loop

        self.secret_key: Optional[str] = secret_key

        self.host: str = host
        self.port: int = port

        self._server: Optional[FastAPI] = None

        self.endpoints: Dict[str, Callable] = {}  # type: ignore

    def get_cls(self, func: RouteFunc) -> Optional[commands.Cog]:
        for cog in self.bot.cogs.values():
            if func.__name__ in dir(cog):
                return cog  # type: ignore
        return self.bot

    def route(self, name: Optional[str] = None) -> Callable:
        """Used to register a coroutine as an endpoint when you have
        access to an instance of :class:`.Server`.

        Parameters
        ----------
        name: str
            The endpoint name. If not provided the method name will be used.
        """

        def decorator(func: Any):
            if not name:
                self.endpoints[func.__name__] = func
            else:
                self.endpoints[name] = func

            return func

        return decorator

    def update_endpoints(self) -> None:
        """Called internally to update the server's endpoints for cog routes."""
        self.endpoints: Dict[Any, Any] = {**self.endpoints, **self.ROUTES}

        self.ROUTES: Dict[Any, Any] = {}

    @unsync
    def __start(self, application: FastAPI):
        self.bot.dispatch('ipc_app_ready', app=application)
        log.info(
            f'[IPC] IPC Application Connection established, running on {self.host}:{self.port}')
        return uvicorn.run(host=self.host, port=self.port, app=application, loop='uvloop', ws='wsproto')

    def start(self):
        self.bot.dispatch('ipc_ready')

        self._server = FastAPI()

        @self._server.websocket_route('/')
        async def run_accept(ws: WebSocket):
            await ws.accept()
            while True:
                data = await ws.receive_json()
                log.debug("Server < %r", data)

                try:
                    endpoint = data['endpoint']
                except KeyError:
                    headers = data.get('headers')

                if not headers or headers.get('Authorization') != self.secret_key: # type: ignore
                    response = {
                        'error': 'Invalid or no token provided.', 'code': 403}
                else:
                    response = {
                        'message': 'Connection success',
                        'port': self.port,
                        'code': 200,
                    }
                log.debug("Server > %r", response)

                await ws.send_json(response)
            else:
                self.update_endpoints()
                endpoint = data['endpoint']
                headers = data.get('headers')
                if not headers or headers.get("Authorization") != self.secret_key:
                    log.info(
                        "Received unauthorized request (Invalid or no token provided).")
                    response = {
                        "error": "Invalid or no token provided.", "code": 403}
                else:
                    if not endpoint or endpoint not in self.endpoints:
                        log.info(
                            "Received invalid request (Invalid or no endpoint given).")
                        response = {
                            "error": "Invalid or no endpoint given.", "code": 400}
                    else:
                        server_response = IpcServerResponse(data)
                    try:
                        attempted_cls = self.bot.cogs.get(
                            self.endpoints[endpoint].__qualname__.split(".")[0]
                        )

                        if attempted_cls:
                            arguments = (attempted_cls, server_response)
                        else:
                            arguments = (server_response,)
                    except AttributeError:
                        # Support base Client
                        arguments = (server_response,)

                    try:
                        ret = await self.endpoints[endpoint](*arguments)
                        response = ret
                    except Exception as error:
                        log.error(
                            "Received error while executing %r with %r",
                            endpoint,
                            data,
                        )
                        self.bot.dispatch("ipc_error", endpoint, error)

                        response = {
                            "error": "IPC route raised error of type {}".format(
                                type(error).__name__
                            ),
                            "code": 500,
                        }
                    try:
                        await ws.send_json(response)
                        log.debug("IPC Server > %r", response)
                    except TypeError as error:
                        if str(error).startswith("Object of type") and str(error).endswith(
                            "is not JSON serializable"
                        ):
                            error_response = (
                                "IPC route returned values which are not able to be sent over sockets."
                                " If you are trying to send a discord.py object,"
                                " please only send the data you need."
                            )
                            log.error(error_response)

                            response = {"error": error_response, "code": 500}

                            await ws.send_json(response)
                            log.debug("IPC Server > %r", response)

                            raise JSONEncodeError(error_response)

        with __import__('contextlib').supress(TypeError):
            try:
                self._server = FastAPI()
                start = self.loop.create_task(
                    self.__start(self._server)  # type: ignore
                )
                log.info(start.result())
            except Exception:
                log.exception('Uh Oh! Something went wrong!', exc_info=True)
