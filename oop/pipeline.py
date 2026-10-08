"""
CQRS & アプリケーションパイプライン モジュール。
書き込みコマンド、Mediator パイプライン、横断的関心事ミドルウェア、
および読み取り専用モデル (マテリアライズド・ビュー) を定義します。
"""
from __future__ import annotations

import abc
import dataclasses
import datetime
import enum
import functools
import logging
import time
import typing

from .domain import (
    CounterAggregateRoot,
    CounterId,
    CounterIncrementedDomainEvent,
    CounterInitializedDomainEvent,
    CounterSessionTerminatedDomainEvent,
    CounterValueExplicitlyAssignedDomainEvent,
    CountBounds,
)
from .repository import ICounterRepository, IEventBus

logger = logging.getLogger(__name__)


# ==============================================================================
# CQRS: リードモデル・プロジェクション (読み取りパス)
# ==============================================================================

@dataclasses.dataclass(frozen=True)
class CounterReadModelView:
    """
    高速参照用の非正規化されたマテリアライズド・ビュー (Read Model)。
    不変なスナップショットとして返すため、参照を受け取った側が書き換えることはできません。
    """
    counter_id: CounterId
    current_count: int = 0
    total_mutations: int = 0
    is_terminated: bool = False
    last_updated: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class ICounterReadModelProjection(abc.ABC):
    @abc.abstractmethod
    def get_view(self, counter_id: CounterId) -> CounterReadModelView | None:
        pass


class InMemoryCounterReadModelProjection(ICounterReadModelProjection):
    """イベントバスを購読してマテリアライズド・ビューをリアクティブに更新するアダプター"""

    def __init__(self, event_bus: IEventBus) -> None:
        self._views: dict[CounterId, CounterReadModelView] = {}
        event_bus.subscribe(CounterInitializedDomainEvent, self._on_initialized)
        event_bus.subscribe(CounterIncrementedDomainEvent, self._on_incremented)
        event_bus.subscribe(CounterValueExplicitlyAssignedDomainEvent, self._on_assigned)
        event_bus.subscribe(CounterSessionTerminatedDomainEvent, self._on_terminated)

    def _on_initialized(self, ev: CounterInitializedDomainEvent) -> None:
        self._views[ev.aggregate_id] = CounterReadModelView(
            counter_id=ev.aggregate_id,
            current_count=ev.initial_value.value,
            last_updated=ev.occurred_on,
        )

    def _on_incremented(self, ev: CounterIncrementedDomainEvent) -> None:
        self._apply_mutation(ev.aggregate_id, ev.new_value.value, ev.occurred_on)

    def _on_assigned(self, ev: CounterValueExplicitlyAssignedDomainEvent) -> None:
        self._apply_mutation(ev.aggregate_id, ev.assigned_value.value, ev.occurred_on)

    def _on_terminated(self, ev: CounterSessionTerminatedDomainEvent) -> None:
        if view := self._views.get(ev.aggregate_id):
            self._views[ev.aggregate_id] = dataclasses.replace(
                view, is_terminated=True, last_updated=ev.occurred_on
            )

    def _apply_mutation(self, counter_id: CounterId, new_count: int, occurred_on: datetime.datetime) -> None:
        if view := self._views.get(counter_id):
            self._views[counter_id] = dataclasses.replace(
                view,
                current_count=new_count,
                total_mutations=view.total_mutations + 1,
                last_updated=occurred_on,
            )

    def get_view(self, counter_id: CounterId) -> CounterReadModelView | None:
        return self._views.get(counter_id)


# ==============================================================================
# CQRS: コマンド & パイプライン実行基盤 (書き込みパス)
# ==============================================================================

@dataclasses.dataclass(frozen=True)
class Command(abc.ABC):
    """書き込み操作の意図を表すマーカーインターフェース"""


@dataclasses.dataclass(frozen=True)
class InitializeCounterCommand(Command):
    aggregate_id: CounterId
    initial_value: int = 0


@dataclasses.dataclass(frozen=True)
class IncrementCounterCommand(Command):
    aggregate_id: CounterId
    step: int = 1


@dataclasses.dataclass(frozen=True)
class UpdateCounterValueCommand(Command):
    aggregate_id: CounterId
    new_value: int


@dataclasses.dataclass(frozen=True)
class TerminateCounterSessionCommand(Command):
    aggregate_id: CounterId


class CommandResult(enum.Enum):
    CONTINUE = enum.auto()
    TERMINATE = enum.auto()


class AggregateNotFoundException(Exception):
    """コマンドの対象となる集約がリポジトリに存在しないことを表す例外"""


CommandHandler: typing.TypeAlias = typing.Callable[[Command], CommandResult]


class ICommandPipelineMiddleware(abc.ABC):
    """横断的関心事を差し込むミドルウェアインターフェース (Chain of Responsibility)"""

    @abc.abstractmethod
    def intercept(self, command: Command, next_stage: CommandHandler) -> CommandResult:
        pass


class TelemetryMetricsMiddleware(ICommandPipelineMiddleware):
    """ナノ秒単位のコマンド実行レイテンシ計測ミドルウェア"""

    def intercept(self, command: Command, next_stage: CommandHandler) -> CommandResult:
        start_ns = time.perf_counter_ns()
        try:
            return next_stage(command)
        finally:
            duration_us = (time.perf_counter_ns() - start_ns) / 1000.0
            logger.debug(
                "コマンドパイプライン実行完了 [%s] 所要時間: %.2f µs",
                command.__class__.__name__,
                duration_us,
            )


class CommandMediator:
    """Mediator パターンによりミドルウェアパイプラインとドメイン操作を調停するクラス"""

    def __init__(
        self,
        repository: ICounterRepository,
        middlewares: typing.Sequence[ICommandPipelineMiddleware] = (),
        bounds: CountBounds | None = None,
    ) -> None:
        self._repository = repository
        self._bounds = bounds or CountBounds()
        # ミドルウェアの入れ子構造は一度だけ組み立てる (先頭のミドルウェアが最も外側)
        self._pipeline: CommandHandler = functools.reduce(
            lambda inner, mw: functools.partial(mw.intercept, next_stage=inner),
            reversed(middlewares),
            self._handle,
        )

    def dispatch(self, command: Command) -> CommandResult:
        return self._pipeline(command)

    def _handle(self, command: Command) -> CommandResult:
        match command:
            case InitializeCounterCommand(aggregate_id, initial_value):
                counter = CounterAggregateRoot.create(aggregate_id, initial_value, self._bounds)
                self._repository.save(counter)
                return CommandResult.CONTINUE

            case IncrementCounterCommand(aggregate_id, step):
                counter = self._load(aggregate_id)
                counter.increment(step)
                self._repository.save(counter)
                return CommandResult.CONTINUE

            case UpdateCounterValueCommand(aggregate_id, new_value):
                counter = self._load(aggregate_id)
                counter.update_explicit(new_value)
                self._repository.save(counter)
                return CommandResult.CONTINUE

            case TerminateCounterSessionCommand(aggregate_id):
                counter = self._repository.get_by_id(aggregate_id)
                if counter and not counter.is_terminated:
                    counter.terminate()
                    self._repository.save(counter)
                return CommandResult.TERMINATE

        raise NotImplementedError(f"未対応のコマンド: {type(command).__name__}")

    def _load(self, aggregate_id: CounterId) -> CounterAggregateRoot:
        counter = self._repository.get_by_id(aggregate_id)
        if counter is None:
            raise AggregateNotFoundException(f"対象の集約ルート [{aggregate_id}] が見つかりません。")
        return counter
