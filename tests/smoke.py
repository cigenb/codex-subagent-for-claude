#!/usr/bin/env python3
"""
协议层冒烟测试：直接用 stdio 和 server.py 对话，覆盖派发 / 续问 / 失败 / 中断 / review / worktree / 退出清理。

用法：python tests/smoke.py
需要本机有可用的 Codex（已登录）。状态目录用临时目录，不影响正在运行的 Claude 会话。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server.py")


class Client:
    def __init__(self, state_dir: str, extra_env=None):
        env = dict(os.environ, CODEX_SUB_STATE_DIR=state_dir, **(extra_env or {}))
        self.p = subprocess.Popen([sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.resp = {}
        self.n = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}})

    def _reader(self):
        for line in self.p.stdout:
            m = json.loads(line.decode("utf-8"))
            if "id" in m:
                self.resp[m["id"]] = m

    def send(self, method, params):
        with self.lock:
            self.n += 1
            i = self.n
            self.p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}, ensure_ascii=False) + "\n").encode())
            self.p.stdin.flush()
        return i

    def get(self, i, timeout=300):
        t = time.time()
        while i not in self.resp:
            if time.time() - t > timeout:
                raise TimeoutError(f"request {i}")
            time.sleep(0.05)
        return self.resp[i]

    def rpc(self, method, params):
        return self.get(self.send(method, params))

    def call_async(self, name, **args):
        return self.send("tools/call", {"name": name, "arguments": args})

    def result(self, i):
        r = self.get(i)["result"]
        return r["content"][0]["text"], bool(r.get("isError"))

    def call(self, name, **args):
        return self.result(self.call_async(name, **args))

    def close(self):
        self.p.stdin.close()
        self.p.wait(15)
        return self.p.returncode


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def thread_of(text):
    return re.search(r"thread ([0-9a-f-]{36})", text).group(1)


def task_of(text):
    return re.search(r"\[([0-9a-f]{8})\]", text).group(1)


def main():
    work = tempfile.mkdtemp(prefix="codex-sub-smoke-")
    state = os.path.join(work, "state")
    repo = os.path.join(work, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    print("workdir:", work)

    print("[1] 启动失败时回收 worktree")
    c = Client(state, {"CODEX_SUB_BIN": os.path.join(work, "no-such-codex.exe")})
    text, err = c.call("codex_spawn", prompt="x", cwd=repo, worktree=True)
    check(err, "codex 不存在时 spawn 报错: " + text[:60])
    c.close()
    wt = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout.strip().splitlines()
    check(len(wt) == 1 and not os.path.isdir(os.path.join(work, "repo.codex-sub")) or not os.listdir(os.path.join(work, "repo.codex-sub")), "worktree 与分支已清理")

    print("[2] 并行首调 / 续问 / 并发续问 / 失败任务")
    c = Client(state)
    i1 = c.call_async("codex_spawn", prompt="只回复 A", cwd=repo, effort="low", wait_s=120)
    i2 = c.call_async("codex_spawn", prompt="只回复 B", cwd=repo, effort="low", wait_s=120)
    a, ea = c.result(i1)
    b, eb = c.result(i2)
    check(not ea and not eb and "completed" in a and "completed" in b, "两个任务并行首调都完成")
    tid = thread_of(a)
    r1 = c.call_async("codex_spawn", prompt="只回复 C", thread_id=tid, cwd=repo, effort="low", wait_s=120)
    r2 = c.call_async("codex_spawn", prompt="只回复 D", thread_id=tid, cwd=repo, effort="low", wait_s=120)
    o1, e1 = c.result(r1)
    o2, e2 = c.result(r2)
    check(sum([e1, e2]) == 1, "并发续问同一 thread：一个成功一个被拒")
    ok_txt = o2 if e1 else o1
    check(thread_of(ok_txt) == tid, "续问跑在同一个 thread 上")
    f, ef = c.call("codex_spawn", prompt="只回复 E", cwd=repo, model="no-such-model", wait_s=60)
    check(not ef and "failed" in f.splitlines()[0], "非法模型 -> 任务 failed 且带错误信息")

    print("[3] 中断 / steer / 状态列表")
    run_txt, _ = c.call("codex_spawn", prompt="从 1 数到 200，每个数字单独一行，慢慢数", cwd=repo, effort="low")
    rid = task_of(run_txt)
    steer_txt, es = c.call("codex_steer", id=rid, prompt="别数了，只回复 停")
    check(not es, "steer 已送达")
    it, ei = c.call("codex_interrupt", id=rid)
    head = it.splitlines()[0]
    check(not ei and head.startswith(f"[{rid}]") and (" interrupted " in head or " completed " in head), "interrupt 后任务结束")
    lst, _ = c.call("codex_status")
    check(lst.count("\n") >= 4, "codex_status 不传 ids 输出列表")

    print("[4] review / worktree")
    with open(os.path.join(repo, "calc.py"), "w", encoding="utf-8") as fh:
        fh.write("def add(a, b):\n    return a - b\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "calc"], cwd=repo, check=True)
    rv, er = c.call("codex_review", scope="custom", value="calc.py 的 add 有没有 bug，一句话", cwd=repo, wait_s=300)
    check(not er and "completed" in rv.splitlines()[0], "codex_review 完成")
    w, ew = c.call("codex_spawn", prompt="修复 calc.py 的 add，改完一句话说明", cwd=repo, worktree=True, effort="low", wait_s=240)
    check(not ew and "branch codex-sub/" in w and "改动文件" in w, "worktree 任务在独立分支完成并报告改动文件")

    print("[5] 退出清理")
    code = c.close()
    check(code == 0, "server 正常退出")
    left = [f for f in os.listdir(state) if f.startswith("session-")]
    check(not left, "状态文件已清理")
    shutil.rmtree(work, ignore_errors=True)
    print("ALL OK")


if __name__ == "__main__":
    main()
