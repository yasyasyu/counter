"""
カウンターの実行エントリーポイント (`python counter/main.py`)。
どの実装も、標準出力の振る舞いは README 1章の共通仕様 (機能 F1〜F6) に従います。

実行方法:
  手続き: python counter/main.py, python counter/main.py --simple
  oop   : python counter/main.py --oop
  fp    : python counter/main.py --fp
  monad : python counter/main.py --monad
"""

import argparse
import sys
import typing
from pathlib import Path

# counter フォルダからの直接実行をサポート
current_dir = str(Path(__file__).resolve().parent)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from monadic_fp.runtime import run_monadic_counter
from oop.container import run_oop_counter
from pythonic_fp import run_pythonic_fp_counter
from simple import run_simple_counter

# 実行モード → (説明, 実行関数)。すべて `run_xxx(initial_count=0)` という同一の契約を持つ。
RUNNERS: dict[str, tuple[str, typing.Callable[[], None]]] = {
    "simple": ("第1部 素朴な手続き型設計 (デフォルト)", run_simple_counter),
    "oop": (
        "第2部 オブジェクト指向アーキテクチャ (DDD / CQRS / イベントソーシング)",
        run_oop_counter,
    ),
    "fp": (
        "第3部 Pythonic な関数型設計 (ストリーム / itertools.accumulate)",
        run_pythonic_fp_counter,
    ),
    "monad": (
        "第4部 純粋関数型アーキテクチャ (State / IO / StateIO モナド)",
        run_monadic_counter,
    ),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="4つの設計パラダイムで実装された同一仕様のカウンター",
        epilog="入力: Enter など → +1 / 数値 → 代入 / c → 終了",
    )
    modes = parser.add_mutually_exclusive_group()
    for mode, (description, _) in RUNNERS.items():
        flags = [f"--{mode}"]
        modes.add_argument(
            *flags, dest="mode", action="store_const", const=mode, help=description
        )
    parser.set_defaults(mode="simple")
    return parser


def main(argv: typing.Sequence[str] | None = None) -> None:
    """
    コマンドライン引数で選んだ実装を実行する。
    """
    args = build_parser().parse_args(argv)
    _, runner = RUNNERS[args.mode]
    runner()


if __name__ == "__main__":
    main()
