"""订单联动入口：python main.py [--once] [--interval 60]。"""
import argparse
import logging
import time

import checkstatus
import getorder
import pto
from request_log import LoggedSession


def process_once(session, cycle=1):
    """共享会话及订单文件，让上一步结果立即进入下一步。"""
    session.note(f"🔄 第 {cycle} 轮", "拉取订单 → 发布开仓 → 同步状态 → 发布平仓")
    results = {}
    steps = (
        ("拉取订单", getorder.process_once, "新增 raw orders"),
        ("发布开仓", pto.pto, "raw orders → pending open orders"),
        ("同步状态", checkstatus.process_once, "pending → finished；其他终态移除"),
        ("发布平仓", pto.inverse_pto, "finished open orders → pending close orders（按来源去重）"),
    )
    for index, (name, action, meaning) in enumerate(steps, 1):
        session.stage = f"第 {cycle} 轮 · {index}/4 {name}"
        session.note(f"▶ {session.stage}", meaning)
        try:
            result = action(session)
        except Exception as exc:
            session.note("❌ 步骤失败", f"{type(exc).__name__}: {exc}")
            logging.exception("%s失败，继续处理其余阶段", name)
            results[name] = {"error": type(exc).__name__}
        else:
            results[name] = result
            session.note("📌 步骤结果", result)
    session.note(f"🏁 第 {cycle} 轮结束", results)
    return results


def main():
    parser = argparse.ArgumentParser(description="联动订单流程；会发送真实 API 请求")
    parser.add_argument("--once", action="store_true", help="只执行一轮")
    parser.add_argument("--interval", type=float, default=60, help="每轮结束后的等待秒数")
    args = parser.parse_args()
    if not 0 < args.interval < float("inf"):
        parser.error("--interval 必须为有限正数")
    logging.basicConfig(level=logging.INFO)
    with LoggedSession() as session:
        cycle = 0
        try:
            while True:
                cycle += 1
                process_once(session, cycle)
                if args.once:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            session.note("⏹ 已停止", "用户中断轮询")


if __name__ == "__main__":
    main()
