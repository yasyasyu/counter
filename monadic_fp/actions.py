"""
代数的データ型 (ADT: Algebraic Data Types) モジュール。
カウンターに対するユーザーの意図を直和型 (Sum Types) として定義します。
"""
from __future__ import annotations

import dataclasses
import typing

from .monads import Nothing, Option, Some

MAX_COUNT = 2**63 - 1  # カウントの上限。これを超える代入・加算は無視する


@dataclasses.dataclass(frozen=True)
class TerminateAction:
    """終了コマンド 'c'"""


@dataclasses.dataclass(frozen=True)
class AssignAction:
    """数値の直接代入コマンド"""
    value: int


@dataclasses.dataclass(frozen=True)
class IncrementAction:
    """フォールバックのカウントアップコマンド"""
    step: int = 1


@dataclasses.dataclass(frozen=True)
class IgnoreAction:
    """上限を超える数値入力 (状態を変えずに継続する)"""


# 直和型: Action は「この 4 つのうちのどれか」であり、それ以外は存在し得ない。
# 閉じた Union なので、match 文の網羅性を型チェッカー (assert_never) で検証できる。
Action: typing.TypeAlias = (
    TerminateAction | AssignAction | IncrementAction | IgnoreAction
)


def _parse_count(digits: str) -> Option[int]:
    """数字列が上限以内なら Some(値)、上限を超えるなら Nothing (ゼロ埋めは許容)"""
    significant = digits.lstrip("0") or "0"
    # 桁数を先に見て、巨大な数字列を int に変換しない (4300 桁超は ValueError)
    if len(significant) > len(str(MAX_COUNT)) or int(significant) > MAX_COUNT:
        return Nothing()
    return Some(int(significant))


def parse_action_pure(raw_input: str) -> Action:
    """
    入力文字列を Action ADT に純粋変換するパーサー。
    範囲チェックはここで済ませるため、AssignAction は常に上限以内の値しか持ちません。
    """
    match raw_input.strip():
        case "c":
            return TerminateAction()
        case q if q.isdecimal():
            return _parse_count(q).map(AssignAction).get_or_else(IgnoreAction())
        case _:
            return IncrementAction(1)
