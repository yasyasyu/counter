"""
第3部: Pythonic な関数型設計 (ストリーム処理)
`while` ループも変数の再代入も使わず、次の3つでカウンターを構成します。
1. 純粋な状態遷移関数 `step_transition` (Reducer)
2. `itertools.accumulate` による入力ストリームの畳み込み (scan)
3. 遅延評価ジェネレータによるパイプライン
"""

import itertools
import typing

MAX_COUNT = 2**63 - 1  # カウントの上限。これを超える代入・加算は無視する


def _parse_count(digits: str, default: int) -> int:
    """数字列が上限以内ならその値を、上限を超えるなら default を返す (ゼロ埋めは許容)"""
    significant = digits.lstrip("0") or "0"
    # 桁数を先に見て、巨大な数字列を int に変換しない (4300 桁超は ValueError)
    if len(significant) > len(str(MAX_COUNT)) or int(significant) > MAX_COUNT:
        return default
    return int(significant)


def step_transition(current_count: int, raw_input: str) -> int | None:
    """
    純粋な状態遷移関数。
    (旧状態, 入力) -> 新状態 (終了時は None)
    副作用がなく参照透過で、同じ引数には必ず同じ結果を返します。

    >>> step_transition(5, "")
    6
    >>> step_transition(5, "42")
    42
    >>> step_transition(5, "c") is None
    True
    >>> step_transition(5, "000000000000000000000042")  # ゼロ埋めは許容
    42
    >>> step_transition(5, "9223372036854775808")  # 上限超えの代入は無視
    5
    >>> step_transition(MAX_COUNT, "")  # 上限からの加算も無視
    9223372036854775807
    """
    match raw_input.strip():
        case "c":
            return None
        case q if q.isdecimal():
            return _parse_count(q, default=current_count)
        case _:
            return min(current_count + 1, MAX_COUNT)


def _reduce(acc: int | None, raw_input: str) -> int | None:
    # 終了 (None) は吸収元: 一度終了したストリームは二度と動き出さない
    return None if acc is None else step_transition(acc, raw_input)


def counter_stream(
    inputs: typing.Iterable[str], initial_count: int = 0
) -> typing.Iterator[int]:
    """
    関数型のストリーム変換パイプライン。
    入力ストリームを `itertools.accumulate` で畳み込み (scan)、
    終了 ('c' による None) が現れた時点で `itertools.takewhile` がストリームを打ち切ります。
    入力は必要になった時点で 1 つずつ読まれます (遅延評価)。

    >>> list(counter_stream(["", "", "50", "", "c", "ignored"]))
    [0, 1, 2, 50, 51]
    >>> list(counter_stream(["", "7"], initial_count=3))
    [3, 4, 7]
    """
    states = itertools.accumulate(inputs, _reduce, initial=initial_count)
    return typing.cast(
        typing.Iterator[int], itertools.takewhile(lambda s: s is not None, states)
    )


def stdin_lines() -> typing.Iterator[str]:
    """標準入力を「行のストリーム」として供給する。EOF / Ctrl+C でストリームが終わる。"""
    try:
        # input() は None を返さないため、EOF / Ctrl+C の例外が出るまで読み続ける
        yield from iter(input, None)
    except (KeyboardInterrupt, EOFError):
        return


def run_pythonic_fp_counter(initial_count: int = 0) -> None:
    """
    対話型 REPL の実行部 (Imperative Shell)。
    ストリームを流れてくる各状態を表示するだけの、薄い外殻です。
    状態は保持せず、表示と入力の順序は遅延評価によって保たれます。
    """
    for count in counter_stream(stdin_lines(), initial_count):
        print(f"count:{count}")
