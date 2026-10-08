"""
第1部: 素朴な手続き型設計 (KISS / YAGNI)
表示・入力・計算を、関数にもクラスにも分けずに、1つの while ループの中に書いた最も単純なカウンター。
"""

# カウントの上限 (2^63 - 1)。これを超える代入・加算は無視する
MAX_COUNT: int = 2**63 - 1


def run_simple_counter(initial_count: int = 0) -> None:
    """表示 → 入力 → 計算 を、1つの while ループで繰り返す (main.py から呼び出す入口)"""
    count = initial_count
    while True:
        print(f"count:{count}")
        try:
            query = input().strip()
        except (KeyboardInterrupt, EOFError):
            break
        if query == "c":
            break
        elif query.isdecimal():
            digits = query.lstrip("0") or "0"  # ゼロ埋めは許容する
            # 桁数を先に見て、巨大な数字列を int に変換しない (4300 桁超は ValueError)
            if len(digits) <= len(str(MAX_COUNT)) and int(digits) <= MAX_COUNT:
                count = int(digits)
        elif count < MAX_COUNT:
            count += 1
