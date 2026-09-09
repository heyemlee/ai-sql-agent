from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

from ai_sql_core import get_settings
from benchmark.run_benchmark import SMOKE_IDS
from benchmark.runner import BenchmarkRunner, run_holdout_recall

"""
发布门禁：每次发布前跑评测，任一指标未达「企业落地合理线」即退出非 0，阻断发布。

卡四项主指标 + 三项对偶护栏（阈值与依据见 thresholds.json / README.md）：

    字段召回率（评测集 / 留出集）  越高越好，护栏：平均进图字段占比
    单库执行准确率                越高越好
    跨库执行准确率                越高越好
    静默错误率                    越低越好，护栏：风险提示误报率

护栏的作用是堵住「刷指标」的路：召回率可以靠全量召回刷满，
静默错误率可以靠把每条结果都标成可疑刷到 0，两者都等于没做。

用法：
    python -m benchmark.gate            # 有 API Key：跑完整 150 条 + 留出集，用 full 阈值
    python -m benchmark.gate --smoke    # 离线：跑冒烟子集，用 smoke 阈值
"""


def _passes(value: float, rule: dict) -> bool:
    if "min" in rule and value < rule["min"]:
        return False
    if "max" in rule and value > rule["max"]:
        return False
    return True


def _format_rule(rule: dict) -> str:
    if "min" in rule:
        return f">= {rule['min']:.2%}"
    return f"<= {rule['max']:.2%}"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI SQL Agent 发布评测门禁")
    parser.add_argument("--smoke", action="store_true", help="离线冒烟模式")
    parser.add_argument(
        "--allow-degrade",
        action="store_true",
        help=(
            "允许静默降级：没有模型凭证时自动转冒烟、缺指标时跳过比较。"
            "**只给本地自查用**——发布门禁必须直接失败，否则一次凭证配错就会"
            "把「跑了 4 条冒烟」当成「通过了发布门禁」。"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "跑 N 轮取均值再判定。模型输出不确定，单次跑分不足以判断达标与否——"
            "实测同一份代码、同一批 25 条跨库题，准确率在 16%%~36%% 之间波动。"
            "**发布门禁建议用 --repeat 3**；默认 1 只适合本地快速自查。"
        ),
    )
    parser.add_argument(
        "--thresholds",
        default=str(Path(__file__).parent / "thresholds.json"),
    )
    args = parser.parse_args()

    # 必须用 Settings，否则配在 .env 里的凭证会被漏掉，
    # 门禁只跑 4 条冒烟就放行，给出虚假信心。
    has_credentials = get_settings().has_model_credentials
    if not args.smoke and not has_credentials:
        if not args.allow_degrade:
            # 这里以前是「自动降级为冒烟门禁」并继续。降级本身不是问题，
            # **静默**才是：CI 日志里那行 stderr 没人看，而退出码是 0，
            # 于是「没配上凭证」和「通过了发布门禁」长得一模一样。
            print(
                "[gate] 未配置模型凭证，无法跑完整门禁。\n"
                "[gate] 发布门禁不做静默降级：要跑冒烟请显式加 --smoke，"
                "本地自查可以加 --allow-degrade。",
                file=sys.stderr,
            )
            return 2
        print("[gate] 未配置模型凭证，按 --allow-degrade 降级为冒烟门禁", file=sys.stderr)
    smoke = args.smoke or not has_credentials

    thresholds_config = json.loads(
        Path(args.thresholds).read_text(encoding="utf-8")
    )
    thresholds = thresholds_config["smoke" if smoke else "full"]

    runner = BenchmarkRunner()
    rounds = max(1, args.repeat)
    samples: dict = {}

    for trial in range(rounds):
        report = runner.run(ids=SMOKE_IDS if smoke else None)
        values = dict(report.grouped_accuracy())
        values["column_recall"] = report.column_recall
        values["avg_column_ratio"] = report.avg_column_ratio
        values["silent_error_rate"] = report.silent_error_rate
        values["false_alarm_rate"] = report.false_alarm_rate
        values["recall_gap"] = report.recall_gap
        # 留出集只测检索，单独跑（40 条口语问法，不参与任何调参）
        if "holdout_recall" in thresholds:
            values["holdout_recall"] = run_holdout_recall(runner.pipeline)
        for key, value in values.items():
            samples.setdefault(key, []).append(value)
        if rounds > 1:
            print(f"[gate] 第 {trial + 1}/{rounds} 轮完成", flush=True)

    measured = {key: mean(values) for key, values in samples.items()}

    print(f"[gate] 模式：{'smoke' if smoke else 'full'}  轮次：{rounds}")
    if rounds == 1 and not smoke:
        print(
            "[gate] ⚠️ 单轮判定。模型输出不确定，单次跑分不足以判断达标与否，"
            "发布前请用 --repeat 3。",
            file=sys.stderr,
        )
    passed = True
    for group, rule in thresholds.items():
        value = measured.get(group)
        if value is None:
            # 「未测得」以前是跳过。一个测不出来的指标和一个不达标的指标，
            # 对「能不能发布」这个问题是同一个答案：不知道就是不能发。
            print(f"[gate] {rule.get('label', group)}: 未测得", file=sys.stderr)
            if not args.allow_degrade:
                passed = False
            continue
        ok = _passes(value, rule)
        passed = passed and ok
        spread = ""
        series = samples.get(group, [])
        if len(series) > 1:
            # 区间必须打出来：均值达标但区间下沿在线下，说明只是这次运气好
            spread = f"  区间 [{min(series):.2%}, {max(series):.2%}]"
        print(
            f"[gate] {rule.get('label', group)}: "
            f"{value:.2%} ({_format_rule(rule)}) {'PASS' if ok else 'FAIL'}{spread}"
        )

    if not passed:
        print("[gate] 未达企业落地合理线，阻断发布", file=sys.stderr)
        return 1

    print("[gate] 通过发布门禁")
    return 0


if __name__ == "__main__":
    sys.exit(main())
