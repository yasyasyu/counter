"""
インフラストラクチャ層（リポジトリ & イベントバス）モジュール。
ヘキサゴナルアーキテクチャのポート＆アダプターにより永続化とイベント配信を担当します。
"""

from __future__ import annotations

import abc
import logging
import typing

from .domain import CounterAggregateRoot, CounterId, CounterSnapshot, DomainEvent

logger = logging.getLogger(__name__)

_E = typing.TypeVar("_E", bound=DomainEvent)


class ConcurrencyConflictException(Exception):
    """楽観的同時実行制御の衝突 (読み込み後に他者が同じ集約を更新していた) を表す例外"""


class ICounterRepository(abc.ABC):
    """永続化の抽象インターフェース (ポート)。Repository パターン"""

    @abc.abstractmethod
    def get_by_id(self, counter_id: CounterId) -> CounterAggregateRoot | None:
        pass

    @abc.abstractmethod
    def save(self, aggregate: CounterAggregateRoot) -> None:
        pass


class IEventBus(abc.ABC):
    """ドメインイベントを配信・購読する抽象ポート。Observer パターン"""

    @abc.abstractmethod
    def publish(self, events: typing.Sequence[DomainEvent]) -> None:
        pass

    @abc.abstractmethod
    def subscribe(
        self, event_type: type[_E], handler: typing.Callable[[_E], None]
    ) -> None:
        pass


class InMemoryEventBus(IEventBus):
    """
    イベントバスの具象実装。同期ブロードキャストを提供します。
    基底クラス (例: DomainEvent) を購読すれば、その派生イベントもすべて受信できます。
    """

    def __init__(self) -> None:
        self._subscribers: dict[type, list[typing.Callable[[typing.Any], None]]] = {}

    def subscribe(
        self, event_type: type[_E], handler: typing.Callable[[_E], None]
    ) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def publish(self, events: typing.Sequence[DomainEvent]) -> None:
        for event in events:
            for event_type in type(event).__mro__:
                for handler in self._subscribers.get(event_type, ()):
                    try:
                        handler(event)
                    except Exception:
                        # 1 つの購読者の失敗が、他の購読者や書き込みパスを巻き込まないよう隔離する
                        logger.exception("イベント通知失敗 [%s]", type(event).__name__)


class InMemoryEventSourcedCounterRepository(ICounterRepository):
    """
    インメモリのイベントストア型永続化アダプター。
    集約オブジェクトそのものではなく「確定事実 (イベント) の列」を保存し、
    読み込みのたびにイベント列を再生 (リハイドレート) して集約を再構築します。
    全履歴の再生は履歴の長さに比例して遅くなるため、`snapshot_interval` 件ごとに
    スナップショットを取り、読み込み時は「最新スナップショット + それ以降のイベント」だけを再生します。
    保存した瞬間に、未コミットイベントを自動でイベントバスへパブリッシュします。
    """

    def __init__(self, event_bus: IEventBus, snapshot_interval: int = 100) -> None:
        if snapshot_interval < 1:
            raise ValueError("snapshot_interval は 1 以上である必要があります")
        self._streams: dict[CounterId, list[DomainEvent]] = {}
        self._snapshots: dict[CounterId, CounterSnapshot] = {}
        self._event_bus: IEventBus = event_bus
        self._snapshot_interval = snapshot_interval

    def get_by_id(self, counter_id: CounterId) -> CounterAggregateRoot | None:
        history = self._streams.get(counter_id)
        if not history:
            return None
        snapshot = self._snapshots.get(counter_id)
        if snapshot is None:
            return CounterAggregateRoot.rehydrate(counter_id, history)
        # 再生するのはスナップショット以降のイベントだけ (高々 snapshot_interval 件)
        return CounterAggregateRoot.from_snapshot(snapshot, history[snapshot.version :])

    def load_history(self, counter_id: CounterId) -> tuple[DomainEvent, ...]:
        """監査用: 集約に起きた全イベントを時系列で返す"""
        return tuple(self._streams.get(counter_id, ()))

    def save(self, aggregate: CounterAggregateRoot) -> None:
        stream = self._streams.get(aggregate.id, [])
        expected_version = aggregate.version - len(aggregate.pending_events)
        if len(stream) != expected_version:
            # 読み込み後に別の誰かが先に保存していた → 上書きせず拒否する (ロストアップデート防止)
            raise ConcurrencyConflictException(
                f"集約 [{aggregate.id}] の同時更新を検知しました"
                f"（期待バージョン: {expected_version}, 実際: {len(stream)}）"
            )
        committed = aggregate.release_uncommitted_events()
        self._streams.setdefault(aggregate.id, []).extend(committed)

        snapshot = self._snapshots.get(aggregate.id)
        if (
            aggregate.version - (snapshot.version if snapshot else 0)
            >= self._snapshot_interval
        ):
            self._snapshots[aggregate.id] = aggregate.to_snapshot()

        self._event_bus.publish(committed)
