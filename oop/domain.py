"""
ドメイン層モジュール。
集約ルート (CounterAggregateRoot)、値オブジェクト (CounterId, CountValue, CountBounds)、
ドメインイベント群、およびビジネスルール違反例外を定義します。

このモジュールは外側のレイヤー (画面・永続化・設定) を一切 import しません (依存性逆転の原則)。
"""
from __future__ import annotations

import abc
import dataclasses
import datetime
import typing
import uuid


class DomainException(Exception):
    """ドメイン層のビジネスルール違反を表す基底例外クラス"""


class InvariantViolationException(DomainException):
    """ドメインの不変条件違反例外"""


@dataclasses.dataclass(frozen=True)
class CounterId:
    """カウンター集約を一意に識別する値オブジェクト (UUIDv4)"""
    value: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.value.version != 4:
            raise InvariantViolationException(
                f"識別子違反: CounterId は UUIDv4 である必要があります（現在値: {self.value}）"
            )

    def __str__(self) -> str:
        return str(self.value)


@dataclasses.dataclass(frozen=True)
class CountValue:
    """カウンターの数値を表現する不変の値オブジェクト"""
    value: int = 0

    def __post_init__(self) -> None:
        if self.value < 0:
            raise InvariantViolationException(
                f"負数カウント違反: カウンター値は 0 以上である必要があります（現在値: {self.value}）"
            )

    def increment(self, step: int = 1) -> CountValue:
        return CountValue(self.value + step)

    def __str__(self) -> str:
        return str(self.value)


@dataclasses.dataclass(frozen=True)
class CountBounds:
    """カウント値の許容範囲を表す値オブジェクト（集約ごとのドメインポリシー）"""
    minimum: int = 0
    maximum: int = 2**63 - 1

    def __post_init__(self) -> None:
        if not 0 <= self.minimum <= self.maximum:
            raise InvariantViolationException(
                f"範囲定義違反: 0 <= 最小値 <= 最大値 である必要があります（{self.minimum}..{self.maximum}）"
            )

    def ensure(self, candidate: CountValue) -> CountValue:
        if not self.minimum <= candidate.value <= self.maximum:
            raise InvariantViolationException(
                f"範囲外カウント違反: カウンター値は {self.minimum}..{self.maximum} の範囲内である必要があります"
                f"（要求値: {candidate.value}）"
            )
        return candidate


@dataclasses.dataclass(frozen=True, kw_only=True)
class DomainEvent(abc.ABC):
    """ドメイン内で発生した確定事実を表す不変イベント基底クラス"""
    aggregate_id: CounterId
    event_id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)
    occurred_on: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class CounterInitializedDomainEvent(DomainEvent):
    initial_value: CountValue
    bounds: CountBounds


@dataclasses.dataclass(frozen=True, kw_only=True)
class CounterIncrementedDomainEvent(DomainEvent):
    previous_value: CountValue
    new_value: CountValue
    step: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class CounterValueExplicitlyAssignedDomainEvent(DomainEvent):
    previous_value: CountValue
    assigned_value: CountValue


@dataclasses.dataclass(frozen=True, kw_only=True)
class CounterSessionTerminatedDomainEvent(DomainEvent):
    final_value: CountValue


@dataclasses.dataclass(frozen=True)
class CounterSnapshot:
    """
    ある時点 (version 件目のイベント適用後) の集約の状態の写し。
    イベント列から常に再計算できる「キャッシュ」であり、正となるデータはあくまでイベント列です。
    """
    aggregate_id: CounterId
    version: int
    value: CountValue
    bounds: CountBounds
    is_terminated: bool


class CounterAggregateRoot:
    """
    集約ルート。
    イベントソーシングにより整合性を維持するエンティティ。
    状態を直接書き換えず、確定事実 (ドメインイベント) を記録・適用することでのみ状態が変化します。
    同じイベント列を `rehydrate` で再生すれば、いつでも同じ集約が再構築されます。
    `from_snapshot` を使えば、スナップショット以降のイベントだけの再生で同じ状態に辿り着けます。
    """

    def __init__(self, aggregate_id: CounterId | None = None) -> None:
        self._id: CounterId = aggregate_id or CounterId()
        self._current_value: CountValue = CountValue(0)
        self._bounds: CountBounds = CountBounds()
        self._initialized: bool = False
        self._terminated: bool = False
        self._uncommitted_events: list[DomainEvent] = []
        self._version: int = 0

    @classmethod
    def create(
        cls,
        aggregate_id: CounterId | None = None,
        initial: int = 0,
        bounds: CountBounds | None = None,
    ) -> CounterAggregateRoot:
        """新しいカウンター集約を生成し、初期化イベントを記録するファクトリ"""
        aggregate = cls(aggregate_id)
        aggregate.initialize(initial, bounds)
        return aggregate

    @classmethod
    def rehydrate(cls, aggregate_id: CounterId, history: typing.Iterable[DomainEvent]) -> CounterAggregateRoot:
        """過去のイベント列 (確定事実の履歴) を再生して集約を再構築する"""
        aggregate = cls(aggregate_id)
        for event in history:
            aggregate._apply_event(event)
        return aggregate

    @classmethod
    def from_snapshot(
        cls, snapshot: CounterSnapshot, newer_events: typing.Iterable[DomainEvent] = ()
    ) -> CounterAggregateRoot:
        """スナップショットの状態から始めて、それ以降のイベントだけを再生して集約を再構築する"""
        aggregate = cls(snapshot.aggregate_id)
        aggregate._current_value = snapshot.value
        aggregate._bounds = snapshot.bounds
        aggregate._initialized = True  # スナップショットは初期化イベントの適用後にしか存在しない
        aggregate._terminated = snapshot.is_terminated
        aggregate._version = snapshot.version
        for event in newer_events:
            aggregate._apply_event(event)
        return aggregate

    def to_snapshot(self) -> CounterSnapshot:
        return CounterSnapshot(
            aggregate_id=self._id,
            version=self._version,
            value=self._current_value,
            bounds=self._bounds,
            is_terminated=self._terminated,
        )

    @property
    def id(self) -> CounterId:
        return self._id

    @property
    def value(self) -> CountValue:
        return self._current_value

    @property
    def version(self) -> int:
        return self._version

    @property
    def is_terminated(self) -> bool:
        return self._terminated

    @property
    def pending_events(self) -> tuple[DomainEvent, ...]:
        """まだ永続化されていないイベント (読み取り専用)"""
        return tuple(self._uncommitted_events)

    def initialize(self, initial: int = 0, bounds: CountBounds | None = None) -> None:
        if self._initialized:
            raise InvariantViolationException("二重初期化違反: カウンターは既に初期化されています")
        bounds = bounds or CountBounds()
        event = CounterInitializedDomainEvent(
            aggregate_id=self._id, initial_value=bounds.ensure(CountValue(initial)), bounds=bounds
        )
        self._record_event(event)

    def increment(self, step: int = 1) -> None:
        self._ensure_active()
        prev = self._current_value
        nxt = self._bounds.ensure(prev.increment(step))
        event = CounterIncrementedDomainEvent(
            aggregate_id=self._id, previous_value=prev, new_value=nxt, step=step
        )
        self._record_event(event)

    def update_explicit(self, target_value: int) -> None:
        self._ensure_active()
        prev = self._current_value
        nxt = self._bounds.ensure(CountValue(target_value))
        event = CounterValueExplicitlyAssignedDomainEvent(
            aggregate_id=self._id, previous_value=prev, assigned_value=nxt
        )
        self._record_event(event)

    def terminate(self) -> None:
        self._ensure_active()
        event = CounterSessionTerminatedDomainEvent(
            aggregate_id=self._id, final_value=self._current_value
        )
        self._record_event(event)

    def _ensure_active(self) -> None:
        if not self._initialized:
            raise InvariantViolationException("未初期化違反: 初期化前のカウンターは操作できません")
        if self._terminated:
            raise InvariantViolationException("終了後操作違反: 終了済みのカウンターは操作できません")

    def _record_event(self, event: DomainEvent) -> None:
        self._apply_event(event)
        self._uncommitted_events.append(event)

    def _apply_event(self, event: DomainEvent) -> None:
        match event:
            case CounterInitializedDomainEvent(initial_value=initial, bounds=bounds):
                self._current_value = initial
                self._bounds = bounds
                self._initialized = True
            case CounterIncrementedDomainEvent(new_value=new_value):
                self._current_value = new_value
            case CounterValueExplicitlyAssignedDomainEvent(assigned_value=assigned):
                self._current_value = assigned
            case CounterSessionTerminatedDomainEvent():
                self._terminated = True
            case _:
                raise TypeError(f"未知のドメインイベント: {type(event).__name__}")
        self._version += 1

    def release_uncommitted_events(self) -> list[DomainEvent]:
        events = list(self._uncommitted_events)
        self._uncommitted_events.clear()
        return events
