"""
依存性注入 (DI) / IoC コンテナモジュール。
全レイヤーの疎結合な依存関係グラフを解決し、アプリケーションを配線します。
"""
from __future__ import annotations

import logging
import sys
import typing

from .config import CounterConfigurationOptions
from .domain import (
    CountBounds,
    CounterIncrementedDomainEvent,
    CounterSessionTerminatedDomainEvent,
    CounterValueExplicitlyAssignedDomainEvent,
)
from .pipeline import (
    CommandMediator,
    ICommandPipelineMiddleware,
    ICounterReadModelProjection,
    InMemoryCounterReadModelProjection,
    TelemetryMetricsMiddleware,
)
from .presentation import (
    CounterApplicationHost,
    IncrementalFallbackStrategy,
    InputStrategyRegistry,
    NumericAssignmentStrategy,
    SessionTerminationStrategy,
)
from .repository import (
    ICounterRepository,
    IEventBus,
    InMemoryEventBus,
    InMemoryEventSourcedCounterRepository,
)

logger = logging.getLogger(__name__)

_T = typing.TypeVar("_T")


class DependencyInjectionContainer:
    """
    最小限の IoC コンテナ。
    サービスは「型 → 生成ファクトリ」として登録され、初回 resolve 時に依存グラフを辿って
    遅延生成されます (以後はシングルトンとしてキャッシュ)。
    """

    def __init__(self) -> None:
        self._factories: dict[type, typing.Callable[[DependencyInjectionContainer], typing.Any]] = {}
        self._singletons: dict[type, typing.Any] = {}

    def register_singleton(self, service_type: type[_T], instance: _T) -> None:
        self._singletons[service_type] = instance

    def register_factory(
        self, service_type: type[_T], factory: typing.Callable[[DependencyInjectionContainer], _T]
    ) -> None:
        self._factories[service_type] = factory

    def resolve(self, service_type: type[_T]) -> _T:
        if service_type not in self._singletons:
            if service_type not in self._factories:
                raise KeyError(f"型 {service_type.__name__} は IoC コンテナに登録されていません。")
            self._singletons[service_type] = self._factories[service_type](self)
        return typing.cast(_T, self._singletons[service_type])


class ServiceContainerFactory:
    """依存関係グラフを登録・配線して DI コンテナを構築するファクトリ (Composition Root)"""

    @staticmethod
    def create_default_container(options: CounterConfigurationOptions) -> DependencyInjectionContainer:
        container = DependencyInjectionContainer()
        container.register_singleton(CounterConfigurationOptions, options)

        # 1. イベントバス (監査ログの購読者を含む)
        container.register_factory(IEventBus, lambda _: ServiceContainerFactory._create_event_bus())

        # 2. リードモデル（プロジェクション）
        container.register_factory(
            ICounterReadModelProjection,
            lambda c: InMemoryCounterReadModelProjection(event_bus=c.resolve(IEventBus)),
        )

        # 3. 永続化リポジトリ (イベントストア)
        container.register_factory(
            ICounterRepository,
            lambda c: InMemoryEventSourcedCounterRepository(
                event_bus=c.resolve(IEventBus),
                snapshot_interval=c.resolve(CounterConfigurationOptions).snapshot_interval,
            ),
        )

        # 4. ドメインポリシー (設定値 → ドメインの値オブジェクトへ変換)
        container.register_factory(
            CountBounds,
            lambda c: CountBounds(
                minimum=c.resolve(CounterConfigurationOptions).minimum_permissible_count,
                maximum=c.resolve(CounterConfigurationOptions).maximum_permissible_count,
            ),
        )

        # 5. コマンド Mediator (+ パイプラインミドルウェア)
        def create_mediator(c: DependencyInjectionContainer) -> CommandMediator:
            middlewares: list[ICommandPipelineMiddleware] = []
            if c.resolve(CounterConfigurationOptions).enable_detailed_metrics:
                middlewares.append(TelemetryMetricsMiddleware())
            return CommandMediator(
                repository=c.resolve(ICounterRepository),
                middlewares=middlewares,
                bounds=c.resolve(CountBounds),
            )

        container.register_factory(CommandMediator, create_mediator)

        # 6. 入力パースストラテジー (数値入力の桁数上限は、許容最大値の桁数から導く)
        container.register_factory(
            InputStrategyRegistry,
            lambda c: InputStrategyRegistry(
                strategies=[
                    SessionTerminationStrategy(),
                    NumericAssignmentStrategy(
                        max_digits=len(
                            str(c.resolve(CounterConfigurationOptions).maximum_permissible_count)
                        )
                    ),
                    IncrementalFallbackStrategy(
                        step=c.resolve(CounterConfigurationOptions).default_increment_step
                    ),
                ]
            ),
        )

        # 7. アプリケーションホスト
        container.register_factory(
            CounterApplicationHost,
            lambda c: CounterApplicationHost(
                options=c.resolve(CounterConfigurationOptions),
                mediator=c.resolve(CommandMediator),
                projection=c.resolve(ICounterReadModelProjection),
                parser=c.resolve(InputStrategyRegistry),
            ),
        )

        # プロジェクションは最初のイベントが発行される前に購読を開始している必要があるため即時生成する
        container.resolve(ICounterReadModelProjection)
        return container

    @staticmethod
    def _create_event_bus() -> IEventBus:
        event_bus = InMemoryEventBus()
        event_bus.subscribe(
            CounterIncrementedDomainEvent,
            lambda ev: logger.debug("ドメインイベント: %s (新値=%s)", type(ev).__name__, ev.new_value),
        )
        event_bus.subscribe(
            CounterValueExplicitlyAssignedDomainEvent,
            lambda ev: logger.debug("ドメインイベント: %s (代入値=%s)", type(ev).__name__, ev.assigned_value),
        )
        event_bus.subscribe(
            CounterSessionTerminatedDomainEvent,
            lambda ev: logger.info("セッションが正常終了しました。最終カウンター値: %s", ev.final_value),
        )
        return event_bus


def run_oop_counter(initial_count: int = 0, banner_enabled: bool = False) -> None:
    """第2部 (オブジェクト指向アーキテクチャ) のカウンターを実行する"""
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="backslashreplace")

    logging.basicConfig(
        level=logging.INFO if banner_enabled else logging.WARNING,
        format="[%(asctime)s.%(msecs)03d] [%(levelname)-8s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    options = CounterConfigurationOptions(initial_count=initial_count, banner_enabled=banner_enabled)
    container = ServiceContainerFactory.create_default_container(options=options)
    container.resolve(CounterApplicationHost).start()
