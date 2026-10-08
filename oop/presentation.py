"""
プレゼンテーション層（入力ストラテジー & アプリケーションホスト）モジュール。
ユーザー入力を CQRS コマンドにパースし、REPL 対話型ループを駆動します。
"""

from __future__ import annotations

import abc
import logging
import sys
import typing

from .config import CounterConfigurationOptions
from .domain import CounterId, DomainException
from .pipeline import (
    Command,
    CommandMediator,
    CommandResult,
    ICounterReadModelProjection,
    IncrementCounterCommand,
    InitializeCounterCommand,
    TerminateCounterSessionCommand,
    UpdateCounterValueCommand,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# 入力パース層 (GoF Strategy パターン)
# ==============================================================================


class IInputParsingStrategy(abc.ABC):
    @abc.abstractmethod
    def can_handle(self, raw_input: str) -> bool:
        pass

    @abc.abstractmethod
    def resolve_command(self, raw_input: str, aggregate_id: CounterId) -> Command:
        pass


class SessionTerminationStrategy(IInputParsingStrategy):
    TERMINATION_TOKEN: typing.Final[str] = "c"

    def can_handle(self, raw_input: str) -> bool:
        return raw_input.strip() == self.TERMINATION_TOKEN

    def resolve_command(self, raw_input: str, aggregate_id: CounterId) -> Command:
        return TerminateCounterSessionCommand(aggregate_id=aggregate_id)


class InputRejectedException(Exception):
    """入力をコマンドに変換できないことを表すプレゼンテーション層の例外"""


class NumericAssignmentStrategy(IInputParsingStrategy):
    def __init__(self, max_digits: int) -> None:
        self._max_digits = max_digits

    def can_handle(self, raw_input: str) -> bool:
        return raw_input.strip().isdecimal()

    def resolve_command(self, raw_input: str, aggregate_id: CounterId) -> Command:
        digits = raw_input.strip().lstrip("0") or "0"  # ゼロ埋めは許容する
        if len(digits) > self._max_digits:
            # 上限超えが確実な巨大な数字列は int に変換せず入口で弾く (4300 桁超は ValueError)
            raise InputRejectedException(
                f"桁数超過: 数値は {self._max_digits} 桁以内で入力してください"
                f"（ゼロ埋めを除き {len(digits)} 桁）"
            )
        return UpdateCounterValueCommand(aggregate_id=aggregate_id, new_value=int(digits))


class IncrementalFallbackStrategy(IInputParsingStrategy):
    def __init__(self, step: int = 1) -> None:
        self._step = step

    def can_handle(self, raw_input: str) -> bool:
        return True

    def resolve_command(self, raw_input: str, aggregate_id: CounterId) -> Command:
        return IncrementCounterCommand(aggregate_id=aggregate_id, step=self._step)


class InputStrategyRegistry:
    def __init__(self, strategies: typing.Sequence[IInputParsingStrategy]) -> None:
        self._strategies = tuple(strategies)

    def parse(self, raw_input: str, aggregate_id: CounterId) -> Command:
        for strategy in self._strategies:
            if strategy.can_handle(raw_input):
                return strategy.resolve_command(raw_input, aggregate_id)
        raise RuntimeError("適用可能な入力ストラテジーが存在しません。")


# ==============================================================================
# アプリケーションホスト (REPL 実行基盤)
# ==============================================================================


class CounterApplicationHost:
    """
    ライフサイクルと REPL ループを統括するホスト。
    依存はすべてコンストラクタで注入され、ホスト自身はコンテナもリポジトリも知りません。
    書き込みは Command として Mediator へ、読み取りは Projection (Read Model) から行います。
    """

    def __init__(
        self,
        options: CounterConfigurationOptions,
        mediator: CommandMediator,
        projection: ICounterReadModelProjection,
        parser: InputStrategyRegistry,
    ) -> None:
        self._options = options
        self._mediator = mediator
        self._projection = projection
        self._parser = parser

    def _render_banner(self) -> None:
        if not self._options.banner_enabled:
            return
        print("カウンター (第2部 オブジェクト指向アーキテクチャ版)", file=sys.stderr)

    def start(self) -> None:
        self._render_banner()
        logger.info("カウンターを初期化しています。")

        aggregate_id = CounterId()
        self._mediator.dispatch(
            InitializeCounterCommand(
                aggregate_id=aggregate_id, initial_value=self._options.initial_count
            )
        )
        logger.info("カウンター集約 [%s] を作成しました。", aggregate_id)

        self._repl_execution_loop(aggregate_id)
        logger.info("アプリケーションを終了しました。")

    def _repl_execution_loop(self, aggregate_id: CounterId) -> None:
        while True:
            print(f"count:{self._query_current_count(aggregate_id)}")
            try:
                result = self._mediator.dispatch(self._read_command(aggregate_id))
            except (InputRejectedException, DomainException) as ex:
                # 入口 (入力形式) またはドメインのルールで拒否された入力は、状態を変えずに対話を続ける
                logger.warning("入力は拒否されました: %s", ex)
                continue
            if result is CommandResult.TERMINATE:
                break

    def _query_current_count(self, aggregate_id: CounterId) -> int:
        view = self._projection.get_view(aggregate_id)
        if view is None:
            raise RuntimeError(f"集約 [{aggregate_id}] のリードモデルが存在しません。")
        return view.current_count

    def _read_command(self, aggregate_id: CounterId) -> Command:
        try:
            raw_input = input()
        except (KeyboardInterrupt, EOFError):
            # 外部割り込み・入力終端も「終了」という確定事実としてイベントに残す
            logger.info("入力の終端または中断を検知したため、終了します。")
            return TerminateCounterSessionCommand(aggregate_id=aggregate_id)
        return self._parser.parse(raw_input, aggregate_id)
