"""
モナドによるカウンターの、計算の合成と実行を行うモジュール。
Action を State モナドの計算に変換し、StateIO (IO の上に State を重ねたモナド) で合成した
プログラム全体の手順を、最も外側で一度だけ実行します。
"""

from __future__ import annotations

import typing

from .actions import (
    MAX_COUNT,
    Action,
    AssignAction,
    IgnoreAction,
    IncrementAction,
    TerminateAction,
    parse_action_pure,
)
from .monads import (
    Nothing,
    Option,
    Some,
    State,
    StateIO,
    print_io,
    read_line_io,
)


def _report_current_state(_: None) -> State[int, Option[int]]:
    return State.get().map(Some)


def action_to_state_computation(action: Action) -> State[int, Option[int]]:
    """
    Action を State モナドの計算に変換する (純粋関数)。
    継続する場合は新しいカウント値を Some で、終了する場合は Nothing を結果として返します。
    """
    match action:
        case TerminateAction():
            return State.pure(Nothing())
        case AssignAction(value):
            return State.put(value).flat_map(_report_current_state)
        case IncrementAction(step):
            return State.modify(lambda s: min(s + step, MAX_COUNT)).flat_map(
                _report_current_state
            )
        case IgnoreAction():
            return _report_current_state(None)
        case _:
            typing.assert_never(action)


def monadic_step() -> StateIO[int, None]:
    """
    REPL の 1 ステップ (表示 → 入力 → ADT 変換 → State 計算) と、
    その先に続く全ステップを StateIO の flat_map で連結した、1つの純粋な式。

    現在のカウントは引数で受け渡さず、StateIO が次の計算・次のステップへ自動的に渡します。
    'c' による Nothing は `get_or_else` で後続の処理を打ち切るため、
    `if` 文も `while` ループも書かずにプログラムが終了します。
    この関数を呼んでも副作用は何も起きず、「手順書」が組み立てられるだけです。
    """
    return (
        StateIO.get()
        .flat_map(lambda count: StateIO.lift_io(print_io(f"count:{count}")))
        .flat_map(lambda _: StateIO.lift_io(read_line_io()))
        .map(parse_action_pure)
        .flat_map(lambda action: StateIO.lift_state(action_to_state_computation(action)))
        .flat_map(
            lambda outcome: outcome.map(lambda _: monadic_step()).get_or_else(
                StateIO.pure(None)
            )
        )
    )


def run_monadic_counter(initial_count: int = 0) -> None:
    """モナドによるカウンターを実行する"""
    program = monadic_step().run(initial_count)  # 初期状態を与えて手順を得る (まだ何も実行されない)
    program.unsafe_run_sync()  # 最も外側で一度だけ、組み立てた手順を実行する
