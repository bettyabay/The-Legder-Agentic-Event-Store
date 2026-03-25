from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class OutboxPublisher(Protocol):
    """Publish an outbox payload to an external destination."""

    async def publish(self, topic: str, message: dict[str, Any]) -> None: ...


class KafkaOutboxPublisher(OutboxPublisher):
    """Kafka publisher implementation (aiokafka optional).

    Requires:
      - aiokafka installed (optional dependency)
      - env: KAFKA_BROKERS and optional KAFKA_TOPIC
    """

    def __init__(self, brokers: list[str], client_id: str = "the-ledger-outbox") -> None:
        self._brokers = brokers
        self._client_id = client_id
        self._producer: Any | None = None

    async def _ensure(self) -> Any:
        if self._producer is not None:
            return self._producer

        try:
            from aiokafka import AIOKafkaProducer  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "aiokafka is required for Kafka outbox publishing. "
                "Install it or disable outbox publisher."
            ) from exc

        self._producer = AIOKafkaProducer(bootstrap_servers=self._brokers, client_id=self._client_id)
        await self._producer.start()
        return self._producer

    async def publish(self, topic: str, message: dict[str, Any]) -> None:  # pragma: no cover
        producer = await self._ensure()
        # Use JSON encoding for payload; message is already dict
        import json

        data = json.dumps(message, default=str).encode("utf-8")
        await producer.send_and_wait(topic, data)

    async def close(self) -> None:  # pragma: no cover
        if self._producer is not None:
            try:
                await self._producer.stop()
            finally:
                self._producer = None


class LoggingOutboxPublisher(OutboxPublisher):
    """Development-only publisher that does not contact Kafka.

    Useful when you want to drain outbox rows without requiring a Kafka broker.
    """

    async def publish(self, topic: str, message: dict[str, Any]) -> None:
        logger.info("[DRY-RUN outbox publish] topic=%s message=%s", topic, message)

