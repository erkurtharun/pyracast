from types import SimpleNamespace

import pika.exceptions  # type: ignore[import-untyped]
import pytest


class FakeRabbitmq:
    """A RabbitMQ server behind pika.BlockingConnection: it can drop its connections or only their
    channels, after which a dropped channel refuses every call, and it can stay unreachable."""

    def __init__(self) -> None:
        self.reachable = True
        self.queues: dict[str, list[bytes]] = {}
        self.channels: list[FakeChannel] = []
        self.attempts = 0

    def connect(self, parameters: object = None) -> FakeConnection:
        self.attempts += 1
        if not self.reachable:
            raise pika.exceptions.AMQPConnectionError("unreachable")
        return FakeConnection(self)

    def drop(self, loss: str = "connection") -> None:
        for channel in self.channels:
            channel.lost = True
            channel.is_open = False
            if loss == "connection":
                channel.connection.is_open = False

    def published(self, queue: str) -> int:
        return len(self.queues.get(queue, []))


class FakeConnection:
    def __init__(self, server: FakeRabbitmq) -> None:
        self.server = server
        self.is_open = True

    def channel(self) -> FakeChannel:
        if not self.is_open:
            raise pika.exceptions.ConnectionWrongStateError("closed")
        channel = FakeChannel(self)
        self.server.channels.append(channel)
        return channel

    def close(self) -> None:
        self.is_open = False


class FakeChannel:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.is_open = True
        self.lost = False
        self.refused = 0

    def _check(self) -> None:
        if self.lost:
            self.refused += 1
            if self.refused == 1:
                raise pika.exceptions.StreamLostError("lost")
            raise pika.exceptions.ChannelWrongStateError("closed")

    def confirm_delivery(self) -> None:
        self._check()

    def queue_declare(self, queue: str, **_options: object) -> SimpleNamespace:
        self._check()
        messages = self.connection.server.queues.setdefault(queue, [])
        return SimpleNamespace(method=SimpleNamespace(message_count=len(messages)))

    def basic_publish(
        self, exchange: str, routing_key: str, body: bytes, **_options: object
    ) -> None:
        self._check()
        self.connection.server.queues.setdefault(routing_key, []).append(body)

    def close(self) -> None:
        self.is_open = False


def install_fake_rabbitmq(monkeypatch: pytest.MonkeyPatch) -> FakeRabbitmq:
    server = FakeRabbitmq()
    monkeypatch.setattr(pika, "BlockingConnection", server.connect)
    return server
