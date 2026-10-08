"""
Options パターンによる設定モジュール。
設定値を1か所にまとめ、構成の変更や環境変数からの注入に備えます。
"""

import dataclasses


@dataclasses.dataclass(frozen=True)
class CounterConfigurationOptions:
    """アプリケーション全体の動作設定を保持する不変(Immutable)クラス"""

    initial_count: int = 0  # セッション開始時のカウント値
    default_increment_step: int = 1  # 1回のインクリメント量
    minimum_permissible_count: int = 0  # 許容される最小カウント値（ドメイン境界）
    maximum_permissible_count: int = (
        2**63 - 1
    )  # 許容される最大カウント値（DB の BIGINT 相当）
    snapshot_interval: int = 100  # 何イベントごとに集約のスナップショットを取るか
    enable_detailed_metrics: bool = True  # ナノ秒単位のレイテンシ計測を行うか
    banner_enabled: bool = True  # 起動時にバナーを表示するか
