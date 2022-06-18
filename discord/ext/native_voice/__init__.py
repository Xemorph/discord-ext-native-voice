from typing import TYPE_CHECKING, Any, Callable, List, Optional, Union

from . import _native_voice as _native

import asyncio
import threading
import logging

from discord import (VoiceProtocol)
from discord.backoff import ExponentialBackoff
from discord.utils import (
    MISSING,
    sane_wait_for
)

if TYPE_CHECKING:
    from discord import (
        abc,
        ClientUser,
        ConnectionState,
        Guild,
        StageChannel,
        VoiceChannel
    )

    from discord.types.voice import (
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload
    )

    VocalGuildChannel = Union[VoiceChannel, StageChannel]

log = logging.getLogger(__name__)


class VoiceClient(VoiceProtocol):
    """Represents a Discord voice connection.
    You do not create these, you typically get them from
    e.g. :meth:`VoiceChannel.connect`.
    Warning
    --------
    In order to use PCM based AudioSources, you must have the opus library
    installed on your system and loaded through :func:`opus.load_opus`.
    Otherwise, your AudioSources must be opus encoded (e.g. using :class:`FFmpegOpusAudio`)
    or the library will not be able to transmit audio.
    Attributes
    -----------
    session_id: :class:`str`
        The voice connection session ID.
    token: :class:`str`
        The voice connection token.
    endpoint: :class:`str`
        The endpoint we are connecting to.
    channel: Union[:class:`VoiceChannel`, :class:`StageChannel`]
        The voice channel connected to.
    """
    channel: VocalGuildChannel
    endpoint_ip: str
    voice_port: int
    ip: str
    port: int
    secret_key: List[int]
    ssrc: int

    def __init__(self, client, channel):
        super().__init__(client, channel)
        state = client._connection
        self.token: str = MISSING
        self.server_id: int = MISSING
        self._connector: _native.VoiceConnector = _native.VoiceConnector()
        self._connector.user_id = client.user.id
        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state: ConnectionState = state
        # this will be used in the AudioPlayer thread
        self._connected: threading.Event = threading.Event()

        self._handshaking: bool = False
        self._potentially_reconnecting: bool = False
        self._voice_state_complete: asyncio.Event = asyncio.Event()
        self._voice_server_complete: asyncio.Event = asyncio.Event()

        self.mode: str = MISSING
        self._connections: int = 0
        self.sequence: int = 0
        self.timestamp: int = 0
        self.timeout: float = 0
        self._runner: asyncio.Task = MISSING
        self._lite_nonce: int = 0

        self._connection: _native.VoiceConnection = None

    @property
    def guild(self) -> Guild:
        """:class:`Guild`: The guild we're connected to."""
        return self.channel.guild

    @property
    def user(self) -> ClientUser:
        """:class:`ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self._state.user  # type: ignore

    # Connection related

    async def on_voice_state_update(self, data: GuildVoiceStatePayload) -> None:
        self.session_id: str = data['session_id']
        self._connector.session_id = self.session_id
        channel_id = data['channel_id']

        if not self._handshaking or self._potentially_reconnecting:
            # If we're done handshaking then we just need to update ourselves
            # If we're potentially reconnecting due to a 4014, then we need to
            # differentiate a channel move and an actual force disconnect
            if channel_id is None:
                # We're being disconnected so cleanup
                await self.disconnect()
            else:
                self.channel = channel_id and self.guild.get_channel(
                    int(channel_id))  # type: ignore
        else:
            self._voice_state_complete.set()

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        if self._voice_server_complete.is_set():
            log.info('Ignoring extraneous voice server update.')
            return

        self.token = data['token']
        self.server_id = int(data['guild_id'])
        endpoint = data.get('endpoint')

        if endpoint is None or self.token is None:
            log.warning(
                'Awaiting endpoint... This requires waiting. '
                'If timeout occurred considering raising the timeout and reconnecting.'
            )
            return

        self.endpoint, _, _ = endpoint.rpartition(':')
        if self.endpoint.startswith('wss://'):
            # Just in case, strip it off since we're going to add it later
            self.endpoint: str = self.endpoint[6:]

        # This gets set later
        self.endpoint_ip = MISSING

        self._connector.update_socket(
            self.token, str(self.server_id), self.endpoint)

        if not self._handshaking:
            # If we're not handshaking then we need to terminate our previous
            # connection in the websocket
            return

        self._voice_server_complete.set()

    async def voice_connect(self, self_deaf: bool = False, self_mute: bool = False) -> None:
        await self.channel.guild.change_voice_state(channel=self.channel, self_deaf=self_deaf, self_mute=self_mute)

    async def voice_disconnect(self) -> None:
        log.info('The voice handshake is being terminated for Channel ID %s (Guild ID %s)',
                 self.channel.id, self.guild.id)
        await self.channel.guild.change_voice_state(channel=None)

    def prepare_handshake(self) -> None:
        self._voice_state_complete.clear()
        self._voice_server_complete.clear()
        self._handshaking = True
        log.info('Starting voice handshake... (connection attempt %d)',
                 self._connections + 1)
        self._connections += 1

    def finish_handshake(self) -> None:
        log.info('Voice handshake complete. Endpoint found %s', self.endpoint)
        self._handshaking = False
        self._voice_server_complete.clear()
        self._voice_state_complete.clear()

    async def connect_socket(self) -> _native.VoiceConnection:
        self._connected.clear()
        _vc = await self._connector.connect(self.loop)
        self._connected.set()
        return _vc

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False) -> None:
        log.info('Connecting to voice...')
        self.timeout = timeout

        for i in range(5):
            self.prepare_handshake()

            # This has to be created before we start the flow.
            futures = [
                self._voice_state_complete.wait(),
                self._voice_server_complete.wait(),
            ]

            # Start the connection flow
            await self.voice_connect(self_deaf=self_deaf, self_mute=self_mute)

            try:
                await sane_wait_for(futures, timeout=timeout)
            except asyncio.TimeoutError:
                await self.disconnect(force=True)
                raise

            self.finish_handshake()

            try:
                self._connection = await self.connect_socket()
                break
            except (_native.ConnectionClosed, asyncio.TimeoutError):
                if reconnect:
                    log.exception('Failed to connect to voice... Retrying...')
                    await asyncio.sleep(1 + i * 2.0)
                    await self.voice_disconnect()
                    continue
                else:
                    raise

        if self._runner is MISSING:
            self._runner = self.client.loop.create_task(
                self.poll_voice_ws(reconnect))

    async def potential_reconnect(self) -> bool:
        # Attempt to stop the player thread from playing early
        self._connected.clear()
        self.prepare_handshake()
        self._potentially_reconnecting = True
        try:
            # We only care about VOICE_SERVER_UPDATE since VOICE_STATE_UPDATE can come before we get disconnected
            await asyncio.wait_for(self._voice_server_complete.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            self._potentially_reconnecting = False
            await self.disconnect(force=True)
            return False

        self.finish_handshake()
        self._potentially_reconnecting = False
        try:
            self._connection = await self._connector.connect(self.loop)
        except (_native.ConnectionClosed, asyncio.TimeoutError):
            return False
        else:
            return True

    @property
    def latency(self) -> float:
        """:class:`float`: Latency between a HEARTBEAT and a HEARTBEAT_ACK in seconds.
        This could be referred to as the Discord Voice WebSocket latency and is
        an analogue of user's voice latencies as seen in the Discord client.
        .. versionadded:: 1.4
        """
        vc = self._connection
        return float("inf") if not vc else vc.latency()

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average of most recent 20 HEARTBEAT latencies in seconds.
        .. versionadded:: 1.4
        """
        vc = self._connection
        return float("inf") if not vc else vc.average_latency()

    async def poll_voice_ws(self, reconnect: bool) -> None:
        backoff = ExponentialBackoff()
        while True:
            try:
                await self._connection.run(self.loop)
            except _native.ConnectionClosed as exc:
                if isinstance(exc, _native.ConnectionClosed):
                    # The following close codes are undocumented so I will document them here.
                    # 1000 - normal closure (obviously)
                    # 4014 - voice channel has been deleted.
                    # 4015 - voice server has crashed
                    if exc.code in (1000, 4015):
                        log.info(
                            f'Disconnecting from voice normally, close code {exc.code}.')
                        await self.disconnect()
                        break
                    if exc.code == 4014:
                        log.info(
                            'Disconnected from voice by force... potentially reconnecting.')
                        successful = await self.potential_reconnect()
                        if not successful:
                            log.info(
                                'Reconnect was unsuccessful, disconnecting from voice normally...')
                            await self.disconnect()
                            break
                        else:
                            continue

                if not reconnect:
                    await self.disconnect()
                    raise

                retry = backoff.delay()
                log.exception(
                    'Disconnected from voice... Reconnecting in %.2fs.', retry)
                self._connected.clear()
                await asyncio.sleep(retry)
                await self.voice_disconnect()
                try:
                    await self.connect(reconnect=True, timeout=self.timeout)
                except asyncio.TimeoutError:
                    # At this point we've retried 5 times...
                    # let's continue the loop.
                    log.warning('Could not connect to voice... Retrying...')
                    continue

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        if not force and not self.is_connected():
            return

        self.stop()
        self._connected.clear()

        try:
            if self._connection:
                await self._connection.disconnect()

            await self.voice_disconnect()
        finally:
            self.cleanup()

    async def move_to(self, channel: Optional[abc.Snowflake]) -> None:
        """|coro|
        Moves you to a different voice channel.
        Parameters
        -----------
        channel: Optional[:class:`abc.Snowflake`]
            The channel to move to. Must be a voice channel.
        """
        await self.channel.guild.change_voice_state(channel=channel)

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connected.is_set()

    # Audio related

    def play(self, title: str,
             after: Optional[Callable[[Optional[Exception]], Any]] = None,
             bitrate: Optional[int] = None) -> None:
        """Plays the given song.

        Parameters
        -----------
        after: Optional[Callable[[Optional[Exception]], Any]]
            A callable function which gets executed after the song have been
            played.
        bitrate: Optional[:class:`int`]
            Sets the bitrate of the opus encoded source. If set then
            the native client will use FFmpegOpusAudio implementation.
        """
        if self._connection:
            self._connection.play(title, after, bitrate)

    def is_playing(self):
        """Indicates if we're currently playing audio."""
        # if self._connection:
        #     return self._connection.is_playing()
        return self._connection is not None and self._connection.is_playing()

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        return self._connection is not None and self._connection.is_paused()

    def stop(self) -> None:
        """Stops playing audio."""
        if self._connection:
            self._connection.stop()

    def pause(self) -> None:
        """Pauses the audio playing."""
        if self._connection:
            self._connection.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        if self._connection:
            self._connection.resume()

    def _debug_info(self) -> Any:
        if self._connection:
            return self._connection.get_state()
        return {}
