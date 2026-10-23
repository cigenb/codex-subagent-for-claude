#!/usr/bin/env python3
"""
Codex-MCP 的 Claude Code hook：把结束的 Codex 任务结果推回派出它的会话。

用法：python hook.py <UserPromptSubmit|PostToolUse|Stop>
- UserPromptSubmit / PostToolUse：有未送达的结果就以 additionalContext 注入
- Stop：有未送达结果 -> 阻止结束并附上结果；还有未 detach 的任务在跑 -> 阻止结束，
  提示去 codex_wait（每个任务最多拦 3 次，避免死循环）

会话归属：状态文件以 Claude 进程 pid 命名（MCP 服务的父进程），hook 沿自己的
祖先链找第一个存在的文件；不依赖环境变量，多个 Claude 窗口互不串。
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import STATE_DIR, ancestors, process_table, server_alive  # noqa: E402

MAX_STOP_BLOCKS = 3
TERMINAL = ("completed", "interrupted", "failed")


def find_session():
    table = process_table()
    for pid in ancestors(os.getpid(), table):
        path = STATE_DIR / f"session-{pid}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None
        if server_alive(table, data.get("server_pid"), pid):
            return path, data
        return None, None  # 文件残留但 MCP 服务已死，交给下次 server 启动时清理
    return None, None


def emit(obj) -> None:
    sys.stdout.buffer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.stdin.buffer.read()  # 输入不需要，但要读完，避免上游写管道阻塞

    path, data = find_session()
    if not path:
        return
    ack_path = path.with_name(path.stem + ".ack.json")
    # 并行工具调用会同时触发多个 hook 进程：拿不到锁就放弃本次，避免同一结果注入两次
    lock_path = path.with_name(path.stem + ".lock")
    try:
        if time.time() - lock_path.stat().st_mtime > 30:
            lock_path.unlink()  # 上一个 hook 被超时杀掉留下的锁
    except OSError:
        pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    os.close(fd)
    try:
        deliver(event, path, data, ack_path)
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def deliver(event: str, path: Path, data, ack_path: Path) -> None:
    try:
        ack = json.loads(ack_path.read_text(encoding="utf-8")) if ack_path.exists() else {}
    except (OSError, ValueError):
        return  # 另一个 hook 正在写，本次放弃，避免重复注入
    ack.setdefault("delivered", [])
    ack.setdefault("stop_blocks", {})

    def save_ack() -> None:
        tmp = ack_path.with_name(f"{ack_path.stem}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(ack), encoding="utf-8")
        os.replace(tmp, ack_path)

    tasks = (data or {}).get("tasks") or {}
    finished = [t for t in tasks.values()
                if t["status"] in TERMINAL and not t.get("reported") and t["id"] not in ack["delivered"]]
    running = [t for t in tasks.values() if t["status"] == "running" and not t.get("detach")]

    if finished:
        text = ("Codex 子代理任务已结束，结果如下（review 结论先给用户看再决定改不改；"
                "\"完成\"不等于验收通过，关键改动自己核对）：\n\n" + "\n\n".join(t["text"] for t in finished))
        if event == "Stop":
            emit({"decision": "block", "reason": text})
        else:
            emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})
        ack["delivered"].extend(t["id"] for t in finished)
        save_ack()
        return

    if event == "Stop" and running:
        blockable = [t for t in running if ack["stop_blocks"].get(t["id"], 0) < MAX_STOP_BLOCKS]
        if not blockable:
            return
        for t in blockable:
            ack["stop_blocks"][t["id"]] = ack["stop_blocks"].get(t["id"], 0) + 1
        save_ack()
        ids = ", ".join(t["id"] for t in blockable)
        emit({"decision": "block", "reason": (
            f"还有 Codex 子代理任务在运行：{ids}。先调用 codex_wait 等它们完成并处理结果，"
            f"再结束；如果确实不需要结果，用 codex_interrupt 中断它们。")})


if __name__ == "__main__":
    main()
