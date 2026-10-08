"""
モナド (Option, State, IO, StateIO) の実装モジュール。
副作用や状態遷移を純粋な値としてカプセル化・合成します。

■ Python とモナド (なぜ自作するのか)
Python の標準ライブラリにモナドは用意されていません。型ヒントに「高カインド型」が無いため、
「Option も State も IO も同じ flat_map を持つ」という共通インターフェース (Monad) を型で表せず、
言語としても例外・None・for・with・async/await で同じ問題を解く流儀だからです。

ただし、標準の中にもモナドの考え方に近いものはあります。

1. リスト内包表記は、リストに対する flat_map そのものです。

    >>> [y for x in [1, 2] for y in (x, x * 10)]
    [1, 10, 2, 20]

2. コルーチンは IO に近い「手順書」です。async 関数を呼んでも中身はまだ実行されず、
   asyncio.run() が unsafe_run_sync の役割を果たします。

    >>> import asyncio
    >>> async def greet() -> str:
    ...     print("実行された")
    ...     return "done"
    >>> recipe = greet()  # 呼んだだけでは何も起きない (IO を組み立てた状態)
    >>> asyncio.run(recipe)  # ここで初めて実行される
    実行された
    'done'

3. Option にあたるのは None ですが、flat_map にあたる操作がありません。
   None を安全にたどる `?.` 演算子を提案した PEP 505 も保留 (Deferred) のままです。

サードパーティには次のようなライブラリがあります (このリポジトリは依存させていません)。

- returns: Maybe, Result, IO, Future, RequiresContext (Reader) と、その組み合わせ (IOResult など)。
  mypy プラグインで型を補います。flat_map は `bind` という名前です。

      # README の鉄道指向プログラミングの例を returns で書くと (各関数は Maybe を返す)
      find_user(user_id).bind(find_order).bind(process_payment).map(send_receipt)

- PyMonad: Maybe, Either, List, Reader, Writer, State など。

どちらにも State を IO の上に重ねた StateIO にあたるものは無く、また仕組みそのものを
見せることが第4部の目的なので、本モジュールでは 4 つのモナドすべてを自作しています。
"""

from __future__ import annotations

import abc
import dataclasses
import typing

_T = typing.TypeVar("_T")
_S = typing.TypeVar("_S")
_A = typing.TypeVar("_A")
_B = typing.TypeVar("_B")


# --- 1. Option モナド (Maybe Monad) ---
class Option(typing.Generic[_T], abc.ABC):
    """値の存在 (Some) または不在 (Nothing) を表すモナド"""

    @abc.abstractmethod
    def map(self, fn: typing.Callable[[_T], _B]) -> Option[_B]:
        pass

    @abc.abstractmethod
    def flat_map(self, fn: typing.Callable[[_T], Option[_B]]) -> Option[_B]:
        pass

    @abc.abstractmethod
    def get_or_else(self, default: _B) -> _T | _B:
        """中身があればそれを、Nothing なら default を返す (Option から値を取り出す)"""


@dataclasses.dataclass(frozen=True)
class Some(Option[_T]):
    value: _T

    def map(self, fn: typing.Callable[[_T], _B]) -> Option[_B]:
        return Some(fn(self.value))

    def flat_map(self, fn: typing.Callable[[_T], Option[_B]]) -> Option[_B]:
        return fn(self.value)

    def get_or_else(self, default: _B) -> _T | _B:
        return self.value


@dataclasses.dataclass(frozen=True)
class Nothing(Option[typing.Any]):
    def map(self, fn: typing.Callable[[typing.Any], _B]) -> Option[_B]:
        return self

    def flat_map(self, fn: typing.Callable[[typing.Any], Option[_B]]) -> Option[_B]:
        return self

    def get_or_else(self, default: _B) -> _B:
        return default


# --- 2. State モナド: S -> (A, S) ---
class State(typing.Generic[_S, _A]):
    """
    状態を直接書き換えるのではなく、「状態 S を受け取り、結果 A と新状態 S を返す関数」
    をカプセル化したモナド。状態遷移を関数合成 (flat_map) で繋ぎ合わせます。
    """

    def __init__(self, run_fn: typing.Callable[[_S], tuple[_A, _S]]) -> None:
        self._run_fn = run_fn

    def run(self, initial_state: _S) -> tuple[_A, _S]:
        return self._run_fn(initial_state)

    @classmethod
    def pure(cls, value: _A) -> State[_S, _A]:
        return cls(lambda s: (value, s))

    def map(self, fn: typing.Callable[[_A], _B]) -> State[_S, _B]:
        def mapped_run(s: _S) -> tuple[_B, _S]:
            a, next_s = self._run_fn(s)
            return fn(a), next_s

        return State(mapped_run)

    def flat_map(self, fn: typing.Callable[[_A], State[_S, _B]]) -> State[_S, _B]:
        def bound_run(s: _S) -> tuple[_B, _S]:
            a, next_s = self._run_fn(s)
            return fn(a).run(next_s)

        return State(bound_run)

    @classmethod
    def get(cls) -> State[_S, _S]:
        return cls(lambda s: (s, s))

    @classmethod
    def put(cls, new_state: _S) -> State[_S, None]:
        return cls(lambda _: (None, new_state))

    @classmethod
    def modify(cls, fn: typing.Callable[[_S], _S]) -> State[_S, None]:
        return cls(lambda s: (None, fn(s)))


# --- 3. IO モナド: () -> A ---
class IO(typing.Generic[_A], abc.ABC):
    """
    副作用（画面出力・キーボード入力）を即時実行せず、
    「後で実行する手順」として保持するモナド。

    IO の値は Pure / Delay / FlatMap の 3 種類のノードからなる「手順書の木構造」にすぎず、
    組み立てている間は何も実行されません。実行は `unsafe_run_sync` だけが担います。
    """

    @staticmethod
    def pure(value: _B) -> IO[_B]:
        return _Pure(value)

    @staticmethod
    def delay(effect: typing.Callable[[], _B]) -> IO[_B]:
        return _Delay(effect)

    def map(self, fn: typing.Callable[[_A], _B]) -> IO[_B]:
        return self.flat_map(lambda a: _Pure(fn(a)))

    def flat_map(self, fn: typing.Callable[[_A], IO[_B]]) -> IO[_B]:
        return _FlatMap(self, fn)

    def unsafe_run_sync(self) -> _A:
        """
        組み立てた手順を実行する。副作用が起きるのは、この呼び出しのときだけです。

        Python には末尾呼び出し最適化が無いため、flat_map を素朴に再帰実行すると、
        すぐに再帰の上限 (既定 1000) に達して RecursionError になります。
        そこで継続 (次に呼ぶ関数) を明示的なスタックに積んで反復的に評価する
        「トランポリン」方式のインタプリタとして実装し、何万回再帰的に flat_map しても
        Python のコールスタックを消費しないようにしています。
        """
        current: IO[typing.Any] = self
        continuations: list[typing.Callable[[typing.Any], IO[typing.Any]]] = []
        while True:
            match current:
                case _FlatMap(source, fn):
                    continuations.append(fn)
                    current = source
                    continue
                case _Pure(value):
                    result = value
                case _Delay(effect):
                    result = effect()
                case _:
                    raise TypeError(f"未知の IO ノード: {current!r}")
            if not continuations:
                return typing.cast(_A, result)
            current = continuations.pop()(result)


@dataclasses.dataclass(frozen=True)
class _Pure(IO[_A]):
    value: _A


@dataclasses.dataclass(frozen=True)
class _Delay(IO[_A]):
    effect: typing.Callable[[], _A]


@dataclasses.dataclass(frozen=True)
class _FlatMap(IO[_B], typing.Generic[_A, _B]):
    source: IO[_A]
    fn: typing.Callable[[_A], IO[_B]]


# --- 4. StateIO モナド: S -> IO[(A, S)] ---
class StateIO(typing.Generic[_S, _A]):
    """
    State を IO の上に重ねたモナド (Haskell の `StateT s IO a`、状態モナド変換子を IO に特化したもの)。

    「状態 S を受け取り、副作用の手順書 IO[(結果 A, 新状態 S)] を返す関数」をカプセル化します。
    flat_map が新しい状態を次の計算へ自動的に渡すため、副作用をまたいでも
    状態を引数で手渡しする必要がありません。
    """

    def __init__(self, run_fn: typing.Callable[[_S], IO[tuple[_A, _S]]]) -> None:
        self._run_fn = run_fn

    def run(self, initial_state: _S) -> IO[tuple[_A, _S]]:
        """初期状態を与えて IO の手順書を得る (この時点ではまだ何も実行されない)"""
        return self._run_fn(initial_state)

    @staticmethod
    def pure(value: _B) -> StateIO[typing.Any, _B]:
        return StateIO(lambda s: IO.pure((value, s)))

    @staticmethod
    def get() -> StateIO[_B, _B]:
        return StateIO(lambda s: IO.pure((s, s)))

    @staticmethod
    def lift_io(io: IO[_B]) -> StateIO[typing.Any, _B]:
        """状態に関係しない IO を、StateIO に持ち上げる (変換する)"""
        return StateIO(lambda s: io.map(lambda b: (b, s)))

    @staticmethod
    def lift_state(state: State[_S, _B]) -> StateIO[_S, _B]:
        """副作用のない純粋な State の計算を、StateIO に持ち上げる (変換する)"""
        return StateIO(lambda s: IO.pure(state.run(s)))

    def map(self, fn: typing.Callable[[_A], _B]) -> StateIO[_S, _B]:
        return StateIO(
            lambda s: self._run_fn(s).map(lambda pair: (fn(pair[0]), pair[1]))
        )

    def flat_map(self, fn: typing.Callable[[_A], StateIO[_S, _B]]) -> StateIO[_S, _B]:
        # 前の計算の新状態 pair[1] を、次の計算の入力状態として自動で接続する
        return StateIO(
            lambda s: self._run_fn(s).flat_map(lambda pair: fn(pair[0]).run(pair[1]))
        )


def print_io(msg: str) -> IO[None]:
    return IO.delay(lambda: print(msg))


def read_line_io() -> IO[str]:
    def safe_input() -> str:
        try:
            return input()
        except (KeyboardInterrupt, EOFError):
            return "c"

    return IO.delay(safe_input)
