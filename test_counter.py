"""
カウンターのテストスイート (`python -m unittest counter/test_counter.py`)。
4つの実装それぞれの単体テストと、README 1章の外部仕様 (機能 F1〜F6) を
4実装すべてが満たすことを確かめる契約テストを中心に、README の作例と main.py の検証を含みます。
"""
from __future__ import annotations

import contextlib
import dataclasses
import doctest
import io
import logging
import re
import sys
import typing
import unittest
import uuid
from pathlib import Path
from unittest import mock

# counter フォルダからの直接実行・テスト探索をサポート
current_dir = str(Path(__file__).resolve().parent)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import main as counter_main
import pythonic_fp
import simple as simple_counter
from monadic_fp import monads
from monadic_fp.actions import (
    MAX_COUNT,
    AssignAction,
    IgnoreAction,
    IncrementAction,
    TerminateAction,
    parse_action_pure,
)
from monadic_fp.monads import IO, Nothing, Some, State, StateIO
from monadic_fp.runtime import action_to_state_computation, run_monadic_counter
from oop.config import CounterConfigurationOptions
from oop.container import ServiceContainerFactory, run_oop_counter
from oop.domain import (
    CountBounds,
    CounterAggregateRoot,
    CounterId,
    CounterSessionTerminatedDomainEvent,
    CountValue,
    DomainEvent,
    InvariantViolationException,
)
from oop.pipeline import (
    Command,
    CommandMediator,
    CommandResult,
    ICommandPipelineMiddleware,
    ICounterReadModelProjection,
    InitializeCounterCommand,
)
from oop.presentation import CounterApplicationHost, InputStrategyRegistry
from oop.repository import (
    ConcurrencyConflictException,
    ICounterRepository,
    IEventBus,
    InMemoryEventBus,
    InMemoryEventSourcedCounterRepository,
)
from pythonic_fp import counter_stream, run_pythonic_fp_counter, step_transition
from simple import run_simple_counter


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None) -> unittest.TestSuite:
    # 純粋関数はドキュメント中の例 (doctest) がそのまま検証可能な仕様になる
    tests.addTests(doctest.DocTestSuite(pythonic_fp))
    # Python 標準の「モナドに近いもの」の例示も、実際に動くことを検証する
    tests.addTests(doctest.DocTestSuite(monads))
    return tests


def run_interactively(
    runner: typing.Callable[[], None],
    lines: typing.Sequence[str],
    end: type[BaseException] = EOFError,
) -> tuple[str, int]:
    """標準入力に lines を流し込み (尽きたら end を送出)、(標準出力, input() の呼び出し回数) を返す"""
    stdout = io.StringIO()
    with mock.patch("builtins.input", side_effect=[*lines, end()]) as fake_input, \
            contextlib.redirect_stdout(stdout):
        runner()
    return stdout.getvalue(), fake_input.call_count


def expected_transcript(*counts: int) -> str:
    return "".join(f"count:{n}\n" for n in counts)


class TestSimpleCounter(unittest.TestCase):
    """第1部 素朴な手続き型設計 (simple.py) の単体テスト"""

    def run_counter(self, lines: list[str], initial_count: int = 0) -> str:
        # 計算は入出力と同じループの中にしかないため、input() を差し替えてループごと動かして確かめる
        transcript, _ = run_interactively(lambda: run_simple_counter(initial_count), lines)
        return transcript

    def test_increment(self) -> None:
        self.assertEqual(self.run_counter(["", "abc"]), expected_transcript(0, 1, 2))

    def test_assignment(self) -> None:
        self.assertEqual(self.run_counter(["42", "0"]), expected_transcript(0, 42, 0))

    def test_termination(self) -> None:
        self.assertEqual(self.run_counter(["c", "unread"], initial_count=10), expected_transcript(10))
        self.assertEqual(self.run_counter(["  c  "], initial_count=10), expected_transcript(10))

    def test_upper_bound(self) -> None:
        maximum = simple_counter.MAX_COUNT
        self.assertEqual(maximum, 2**63 - 1)
        self.assertEqual(
            self.run_counter([str(maximum), "", str(2**63), "1" * 5000, "0" * 5000 + "7"]),
            # 上限からの加算・上限超えの代入・int 変換できない桁数は無視し、ゼロ埋めは許容する
            expected_transcript(0, maximum, maximum, maximum, maximum, 7),
        )

    def test_readme_shows_the_whole_simple_py(self) -> None:
        # README 第1部は、問いの前に simple.py の全文を示している。実装と食い違っていないかを確かめる
        readme = (Path(current_dir) / "README.md").read_text(encoding="utf-8")
        source = (Path(current_dir) / "simple.py").read_text(encoding="utf-8")
        self.assertIn(f"```python\n{source}```", readme)


class TestOOPCounter(unittest.TestCase):
    """第2部 オブジェクト指向アーキテクチャ (oop/) の単体テスト"""

    def test_value_object_invariants(self) -> None:
        val = CountValue(10)
        self.assertEqual(val.value, 10)
        self.assertEqual(val.increment(5).value, 15)
        with self.assertRaises(InvariantViolationException):
            CountValue(-1)

    def test_counter_id_must_be_uuid4(self) -> None:
        self.assertEqual(CounterId().value.version, 4)
        with self.assertRaises(InvariantViolationException):
            CounterId(uuid.uuid1())

    def test_aggregate_root_event_sourcing(self) -> None:
        aggregate = CounterAggregateRoot()
        aggregate.initialize(0)
        self.assertEqual(aggregate.value.value, 0)
        aggregate.increment(2)
        self.assertEqual(aggregate.value.value, 2)
        aggregate.update_explicit(50)
        self.assertEqual(aggregate.value.value, 50)
        aggregate.terminate()

        events = aggregate.release_uncommitted_events()
        self.assertEqual(len(events), 4)
        self.assertEqual(aggregate.pending_events, ())

        # 同じイベント列を再生すれば、同じ集約が再構築される
        replayed = CounterAggregateRoot.rehydrate(aggregate.id, events)
        self.assertEqual(replayed.value, aggregate.value)
        self.assertEqual(replayed.version, aggregate.version)
        self.assertTrue(replayed.is_terminated)

    def test_aggregate_rejects_operations_outside_its_lifecycle(self) -> None:
        with self.assertRaises(InvariantViolationException):
            CounterAggregateRoot().increment()  # 未初期化

        aggregate = CounterAggregateRoot.create()
        with self.assertRaises(InvariantViolationException):
            aggregate.initialize(0)  # 二重初期化

        aggregate.terminate()
        with self.assertRaises(InvariantViolationException):
            aggregate.increment()  # 終了後
        with self.assertRaises(InvariantViolationException):
            aggregate.update_explicit(1)

    def test_aggregate_enforces_bounds(self) -> None:
        aggregate = CounterAggregateRoot.create(initial=9, bounds=CountBounds(minimum=0, maximum=10))
        aggregate.increment()
        self.assertEqual(aggregate.value.value, 10)
        with self.assertRaises(InvariantViolationException):
            aggregate.increment()
        with self.assertRaises(InvariantViolationException):
            aggregate.update_explicit(11)
        self.assertEqual(aggregate.value.value, 10)  # 拒否された操作は状態を変えない
        with self.assertRaises(InvariantViolationException):
            CountBounds(minimum=5, maximum=4)

    def test_repository_stores_events_and_rehydrates(self) -> None:
        repo = InMemoryEventSourcedCounterRepository(event_bus=InMemoryEventBus())
        aggregate = CounterAggregateRoot.create(initial=3)
        aggregate.increment()
        repo.save(aggregate)

        loaded = repo.get_by_id(aggregate.id)
        assert loaded is not None
        self.assertIsNot(loaded, aggregate)  # オブジェクトではなくイベントから再構築される
        self.assertEqual(loaded.value.value, 4)
        self.assertEqual(loaded.version, 2)
        self.assertEqual(len(repo.load_history(aggregate.id)), 2)
        self.assertIsNone(repo.get_by_id(CounterId()))

    def test_repository_replays_only_events_after_snapshot(self) -> None:
        repo = InMemoryEventSourcedCounterRepository(
            event_bus=InMemoryEventBus(), snapshot_interval=10
        )
        aggregate = CounterAggregateRoot.create()
        repo.save(aggregate)
        for _ in range(24):
            counter = repo.get_by_id(aggregate.id)
            assert counter is not None
            counter.increment()
            repo.save(counter)

        with mock.patch.object(
            CounterAggregateRoot,
            "_apply_event",
            autospec=True,
            side_effect=CounterAggregateRoot._apply_event,
        ) as apply_event:
            loaded = repo.get_by_id(aggregate.id)
        assert loaded is not None
        # 全 25 件ではなく、スナップショット (20 件目) 以降の 5 件だけを再生する
        self.assertEqual(apply_event.call_count, 5)

        # スナップショット経由でも、全履歴の再生と同じ集約になる
        replayed = CounterAggregateRoot.rehydrate(aggregate.id, repo.load_history(aggregate.id))
        self.assertEqual((loaded.value.value, loaded.version), (24, 25))
        self.assertEqual((loaded.value, loaded.version), (replayed.value, replayed.version))

    def test_repository_detects_lost_update(self) -> None:
        repo = InMemoryEventSourcedCounterRepository(event_bus=InMemoryEventBus())
        aggregate = CounterAggregateRoot.create()
        repo.save(aggregate)

        first = repo.get_by_id(aggregate.id)
        second = repo.get_by_id(aggregate.id)
        assert first is not None and second is not None
        first.increment()
        second.update_explicit(99)
        repo.save(first)
        with self.assertRaises(ConcurrencyConflictException):
            repo.save(second)  # 古いバージョンを基にした上書きは拒否される

        latest = repo.get_by_id(aggregate.id)
        assert latest is not None
        self.assertEqual(latest.value.value, 1)

    def test_event_bus_delivers_to_base_class_subscribers(self) -> None:
        bus = InMemoryEventBus()
        audit_log: list[DomainEvent] = []
        bus.subscribe(DomainEvent, audit_log.append)
        aggregate = CounterAggregateRoot.create()
        aggregate.increment()
        bus.publish(aggregate.release_uncommitted_events())
        self.assertEqual(len(audit_log), 2)

    def test_middlewares_wrap_handler_in_declared_order(self) -> None:
        calls: list[str] = []

        class Recording(ICommandPipelineMiddleware):
            def __init__(self, name: str) -> None:
                self.name = name

            def intercept(self, command: Command, next_stage: typing.Callable[[Command], CommandResult]) -> CommandResult:
                calls.append(f"{self.name}:before")
                try:
                    return next_stage(command)
                finally:
                    calls.append(f"{self.name}:after")

        repo = InMemoryEventSourcedCounterRepository(event_bus=InMemoryEventBus())
        mediator = CommandMediator(repo, middlewares=[Recording("outer"), Recording("inner")])
        mediator.dispatch(InitializeCounterCommand(aggregate_id=CounterId()))
        self.assertEqual(calls, ["outer:before", "inner:before", "inner:after", "outer:after"])

    def test_cqrs_pipeline_and_projection(self) -> None:
        options = CounterConfigurationOptions(banner_enabled=False)
        container = ServiceContainerFactory.create_default_container(options=options)

        projection = container.resolve(ICounterReadModelProjection)
        mediator = container.resolve(CommandMediator)
        parser = container.resolve(InputStrategyRegistry)
        self.assertIs(container.resolve(ICounterRepository), container.resolve(ICounterRepository))

        agg_id = CounterId()
        mediator.dispatch(InitializeCounterCommand(aggregate_id=agg_id, initial_value=0))

        view = projection.get_view(agg_id)
        assert view is not None
        self.assertEqual(view.current_count, 0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.current_count = 999  # type: ignore[misc]  # リードモデルは外から書き換えられない

        self.assertEqual(mediator.dispatch(parser.parse("", agg_id)), CommandResult.CONTINUE)
        self.assertEqual(projection.get_view(agg_id).current_count, 1)  # type: ignore[union-attr]

        self.assertEqual(mediator.dispatch(parser.parse("77", agg_id)), CommandResult.CONTINUE)
        latest = projection.get_view(agg_id)
        assert latest is not None
        self.assertEqual(latest.current_count, 77)
        self.assertEqual(latest.total_mutations, 2)
        self.assertEqual(view.current_count, 0)  # 以前に受け取ったスナップショットは不変のまま

        self.assertEqual(mediator.dispatch(parser.parse("c", agg_id)), CommandResult.TERMINATE)
        self.assertTrue(projection.get_view(agg_id).is_terminated)  # type: ignore[union-attr]

    def test_host_rejects_rule_violation_and_keeps_running(self) -> None:
        options = CounterConfigurationOptions(banner_enabled=False, maximum_permissible_count=100)
        host = ServiceContainerFactory.create_default_container(options).resolve(
            CounterApplicationHost
        )
        with self.assertLogs("oop", level="WARNING") as logs:
            transcript, _ = run_interactively(host.start, ["500", "1" * 5000, "", "c"])
        self.assertEqual(transcript, expected_transcript(0, 0, 0, 1))
        self.assertIn("範囲外カウント違反", logs.output[0])  # ドメインで拒否
        self.assertIn("桁数超過", logs.output[1])  # int 変換する前に入口で拒否

    def test_host_records_termination_on_eof(self) -> None:
        container = ServiceContainerFactory.create_default_container(
            CounterConfigurationOptions(banner_enabled=False)
        )
        terminated: list[CounterSessionTerminatedDomainEvent] = []
        container.resolve(IEventBus).subscribe(CounterSessionTerminatedDomainEvent, terminated.append)

        run_interactively(container.resolve(CounterApplicationHost).start, ["", ""])
        self.assertEqual(len(terminated), 1)
        self.assertEqual(terminated[0].final_value, CountValue(2))


class TestPythonicFPCounter(unittest.TestCase):
    """第3部 Pythonic な関数型設計 (pythonic_fp.py) の単体テスト"""

    def test_step_transition(self) -> None:
        self.assertEqual(step_transition(0, ""), 1)
        self.assertEqual(step_transition(10, "foo"), 11)
        self.assertEqual(step_transition(0, "42"), 42)
        self.assertIsNone(step_transition(10, "c"))
        self.assertIsNone(step_transition(10, "  c  "))
        self.assertEqual(step_transition(3, "1" * 5000), 3)
        self.assertEqual(step_transition(3, "0" * 5000 + "7"), 7)

    def test_counter_stream_pipeline(self) -> None:
        # 入力シーケンスから累積された状態ストリームを生成
        inputs = ["", "", "50", "", "c", "ignored"]
        result = list(counter_stream(inputs, initial_count=0))
        # 初期値 0 -> (+1)=1 -> (+1)=2 -> (=50) -> (+1)=51 -> 'c'で終了
        self.assertEqual(result, [0, 1, 2, 50, 51])

    def test_counter_stream_is_lazy(self) -> None:
        consumed: list[str] = []

        def tracked_inputs() -> typing.Iterator[str]:
            for line in ["", "c", "never-read"]:
                consumed.append(line)
                yield line

        stream = counter_stream(tracked_inputs())
        self.assertEqual(next(stream), 0)
        self.assertEqual(consumed, [])  # 初期値を得るのに入力は 1 つも読まれない
        self.assertEqual(list(stream), [1])
        self.assertEqual(consumed, ["", "c"])  # 終了後の入力は読まれない


class TestMonadicCounter(unittest.TestCase):
    """第4部 純粋関数型アーキテクチャ (monadic_fp/) の単体テスト"""

    def test_action_parsing(self) -> None:
        self.assertIsInstance(parse_action_pure("c"), TerminateAction)
        self.assertIsInstance(parse_action_pure("  c  "), TerminateAction)
        self.assertEqual(parse_action_pure("42"), AssignAction(42))
        self.assertEqual(parse_action_pure(""), IncrementAction(1))
        self.assertEqual(parse_action_pure("hello"), IncrementAction(1))
        self.assertEqual(parse_action_pure(str(MAX_COUNT)), AssignAction(MAX_COUNT))
        self.assertEqual(parse_action_pure(str(MAX_COUNT + 1)), IgnoreAction())
        self.assertEqual(parse_action_pure("1" * 5000), IgnoreAction())
        self.assertEqual(parse_action_pure("0" * 5000 + "7"), AssignAction(7))

    def test_state_computation(self) -> None:
        inc_comp = action_to_state_computation(IncrementAction(1))
        res, next_state = inc_comp.run(5)
        self.assertEqual(res, Some(6))
        self.assertEqual(next_state, 6)

        assign_comp = action_to_state_computation(AssignAction(99))
        res, next_state = assign_comp.run(5)
        self.assertEqual(res, Some(99))
        self.assertEqual(next_state, 99)

        term_comp = action_to_state_computation(TerminateAction())
        res, next_state = term_comp.run(5)
        self.assertEqual(res, Nothing())
        self.assertEqual(next_state, 5)

        ignore_comp = action_to_state_computation(IgnoreAction())
        self.assertEqual(ignore_comp.run(5), (Some(5), 5))

        self.assertEqual(inc_comp.run(MAX_COUNT), (Some(MAX_COUNT), MAX_COUNT))

    def test_state_computation_is_exhaustive(self) -> None:
        with self.assertRaises(AssertionError):
            action_to_state_computation(object())  # type: ignore[arg-type]

    def test_option_short_circuits(self) -> None:
        self.assertEqual(Some(1).map(lambda x: x + 1).get_or_else(0), 2)
        self.assertEqual(Nothing().map(lambda x: x + 1).get_or_else(0), 0)
        self.assertEqual(Some(1).flat_map(lambda _: Nothing()), Nothing())

    def test_state_monad_threads_state(self) -> None:
        program = State.modify(lambda s: s * 10).flat_map(lambda _: State.get())
        self.assertEqual(program.run(4), (40, 40))

    def test_state_io_threads_state_across_effects(self) -> None:
        effects: list[int] = []
        program = (
            StateIO.lift_state(State.modify(lambda s: s + 1))
            .flat_map(lambda _: StateIO.get())
            .flat_map(lambda n: StateIO.lift_io(IO.delay(lambda: effects.append(n))))
            .flat_map(lambda _: StateIO.lift_state(State.modify(lambda s: s * 2)))
            .flat_map(lambda _: StateIO.get())
        )
        recipe = program.run(20)
        self.assertEqual(effects, [])  # 初期状態を与えても、手順書ができるだけで何も起きない
        self.assertEqual(recipe.unsafe_run_sync(), (42, 42))
        self.assertEqual(effects, [21])  # 副作用は途中の状態 (21) を自動的に受け取っている

    def test_state_io_is_stack_safe(self) -> None:
        def countdown() -> StateIO[int, str]:
            return StateIO.get().flat_map(
                lambda n: StateIO.pure("done")
                if n == 0
                else StateIO.lift_state(State.put(n - 1)).flat_map(lambda _: countdown())
            )

        self.assertEqual(countdown().run(100_000).unsafe_run_sync(), ("done", 0))

    def test_io_is_lazy_until_unsafe_run_sync(self) -> None:
        effects: list[str] = []
        program = IO.delay(lambda: effects.append("fired")).map(lambda _: 42)
        self.assertEqual(effects, [])  # 組み立てただけでは何も起きない
        self.assertEqual(program.unsafe_run_sync(), 42)
        self.assertEqual(effects, ["fired"])

    def test_io_is_stack_safe(self) -> None:
        depth = 100_000  # Python の再帰上限 (既定 1000) を大きく超える

        def countdown(n: int) -> IO[int]:
            return IO.pure(n).flat_map(lambda k: IO.pure(k) if k == 0 else countdown(k - 1))

        self.assertEqual(countdown(depth).unsafe_run_sync(), 0)

        left_nested: IO[int] = IO.pure(0)
        for _ in range(depth):
            left_nested = left_nested.map(lambda x: x + 1)
        self.assertEqual(left_nested.unsafe_run_sync(), depth)

    def test_long_session_does_not_overflow(self) -> None:
        transcript, _ = run_interactively(run_monadic_counter, [""] * 5_000 + ["c"])
        self.assertTrue(transcript.endswith("count:5000\n"))


# README 1章「4実装が守る共通の外部仕様」で定義した、システムとして要求する機能
FEATURES: dict[str, str] = {
    "F1": "現在値の表示",
    "F2": "加算",
    "F3": "代入",
    "F4": "上限の維持",
    "F5": "終了",
    "F6": "入力終端・中断での終了",
}


@dataclasses.dataclass(frozen=True)
class ContractScenario:
    """外部仕様の機能を検証する、1回分の対話セッション"""
    name: str
    features: frozenset[str]           # このシナリオが検証する機能 ID
    lines: tuple[str, ...]             # 標準入力に流し込む行
    expected_counts: tuple[int, ...]   # 標準出力に表示されるべきカウントの列
    expected_reads: int                # input() が呼ばれるべき回数 (終了後の入力は読まない)
    end: type[BaseException] = EOFError  # 行が尽きたあとに起きること (EOF または Ctrl+C)

    @property
    def label(self) -> str:
        return f"[{', '.join(sorted(self.features))}] {self.name}"


class TestUnifiedContract(unittest.TestCase):
    """4つの実装すべてが、外部仕様の機能 F1〜F6 を同じように実現していることを検証"""

    RUNNERS: typing.ClassVar[dict[str, typing.Callable[[], None]]] = {
        "simple": run_simple_counter,
        "oop": run_oop_counter,
        "fp": run_pythonic_fp_counter,
        "monad": run_monadic_counter,
    }

    SCENARIOS: typing.ClassVar[tuple[ContractScenario, ...]] = (
        ContractScenario(
            "加算・代入・終了 (終了後の入力は読まない)",
            frozenset({"F1", "F2", "F3", "F5"}),
            ("", "", "50", "", "c", "unread"),
            (0, 1, 2, 50, 51),
            5,
        ),
        ContractScenario(
            "前後に空白のある c で即終了",
            frozenset({"F1", "F5"}),
            ("  c  ",),
            (0,),
            1,
        ),
        ContractScenario(
            "数字以外・負数・大文字 C・空白だけは +1、前後に空白のある数字は代入",
            frozenset({"F2", "F3"}),
            ("abc", " 3 ", "-5", "C", "   ", "c"),
            (0, 1, 3, 4, 5, 6),
            6,
        ),
        ContractScenario(
            "ゼロ埋めと全角数字も数字として代入",
            frozenset({"F3"}),
            ("0" * 20 + "42", "0" * 5000 + "7", "0" * 30, "１２", "c"),
            (0, 42, 7, 0, 12),
            5,
        ),
        ContractScenario(
            "上限 2^63-1 を超える代入と、上限からの加算は無視",
            frozenset({"F4"}),
            ("5", str(2**63), "1" * 5000, "000" + str(2**63), str(2**63 - 1), "", "c"),
            (0, 5, 5, 5, 5, 2**63 - 1, 2**63 - 1),
            7,
        ),
        ContractScenario(
            "c なしの EOF で正常終了",
            frozenset({"F6"}),
            ("", "7"),
            (0, 1, 7),
            3,
        ),
        ContractScenario(
            "Ctrl+C で正常終了",
            frozenset({"F6"}),
            ("", "7"),
            (0, 1, 7),
            3,
            end=KeyboardInterrupt,
        ),
    )

    def setUp(self) -> None:
        # 契約の対象は標準出力だけ (標準エラー出力は各実装の自由)。OOP 版の警告ログはここでは抑止する
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_every_feature_has_a_scenario(self) -> None:
        covered = set().union(*(scenario.features for scenario in self.SCENARIOS))
        self.assertEqual(covered, set(FEATURES), "シナリオが参照する機能 ID が、定義済みの機能と一致しません")

    def test_all_paradigms_realize_the_same_features(self) -> None:
        for mode, runner in self.RUNNERS.items():
            for scenario in self.SCENARIOS:
                with self.subTest(mode=mode, scenario=scenario.label):
                    transcript, reads = run_interactively(runner, scenario.lines, scenario.end)
                    self.assertEqual(transcript, expected_transcript(*scenario.expected_counts))
                    self.assertEqual(reads, scenario.expected_reads)


class TestReadmeExercises(unittest.TestCase):
    """README の「試してみよう」の作例 (第1部・第2部の count_from) が、変更依頼 R1 を満たすことを検証"""

    # R1: 入力の列を、F2〜F5 と同じルールで処理したときの最後のカウントを返す (列の終わりは終了)
    R1_CASES: typing.ClassVar[list[list[str]]] = [
        ["", "", "50", "c"],
        ["", "", "50", "", "c", "unread"],
        ["abc", " 3 ", "-5", "C", "   "],
        ["0" * 20 + "42", "0" * 5000 + "7", "１２"],
        ["5", str(2**63), "1" * 5000, str(2**63 - 1), ""],
        [],
    ]

    @classmethod
    def setUpClass(cls) -> None:
        readme = (Path(current_dir) / "README.md").read_text(encoding="utf-8")
        blocks = [b for b in re.findall(r"```python\n(.*?)```", readme, re.S) if "def count_from" in b]
        assert len(blocks) == 2, "README に count_from の作例が2つ (第1部・第2部) あるはず"
        simple_ns: dict[str, typing.Any] = {"MAX_COUNT": simple_counter.MAX_COUNT}  # my_simple.py に写した前提
        exec(blocks[0], simple_ns)
        oop_ns: dict[str, typing.Any] = {}
        with contextlib.redirect_stdout(io.StringIO()):
            exec(blocks[1], oop_ns)
        cls.implementations = {"第1部": simple_ns["count_from"], "第2部": oop_ns["count_from"]}

    def test_count_from_examples_satisfy_r1(self) -> None:
        for lines in self.R1_CASES:
            # 基準: F1〜F6 を満たす run_simple_counter に同じ入力を流したときの、最後に表示されたカウント
            transcript, _ = run_interactively(run_simple_counter, lines)
            expected = int(transcript.splitlines()[-1].removeprefix("count:"))
            for part, count_from in self.implementations.items():
                with self.subTest(part=part, lines=[line[:20] for line in lines]):
                    self.assertEqual(count_from(lines), expected)


class TestMainEntryPoint(unittest.TestCase):
    """統合実行エントリーポイント (main.py) のモード切り替えテスト"""

    def assert_dispatches(self, argv: list[str], expected_mode: str) -> None:
        fakes = {mode: mock.Mock(name=mode) for mode in counter_main.RUNNERS}
        patched = {mode: (desc, fakes[mode]) for mode, (desc, _) in counter_main.RUNNERS.items()}
        with mock.patch.dict(counter_main.RUNNERS, patched):
            counter_main.main(argv)
        for mode, fake in fakes.items():
            self.assertEqual(fake.called, mode == expected_mode, f"argv={argv}, mode={mode}")

    def test_mode_selection(self) -> None:
        self.assert_dispatches([], "simple")
        self.assert_dispatches(["--simple"], "simple")
        self.assert_dispatches(["--oop"], "oop")
        self.assert_dispatches(["--fp"], "fp")
        self.assert_dispatches(["--monad"], "monad")

    def test_conflicting_or_unknown_flags_are_rejected(self) -> None:
        for argv in (["--oop", "--fp"], ["--unknown"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    counter_main.main(argv)


if __name__ == "__main__":
    unittest.main()
