#!/usr/bin/env python3
"""
Codex-MCP -- 把 Codex 当子代理用的 MCP 服务（Windows，仅标准库）

一个常驻的 `codex app-server` 子进程承载所有任务：每个任务一个 thread，
派发后立即返回，状态靠事件流实时更新，支持中途 steer / interrupt / 续问。
任务状态写入状态文件，由 hook.py 推回派出它的 Claude 会话。
"""

import ctypes
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.2.7"

STATE_DIR = Path(os.environ.get("CODEX_SUB_STATE_DIR") or (
    Path(os.environ.get("LOCALAPPDATA", Path.home())) / "codex-sub"))
LOG_FILE = STATE_DIR / "server.log"

# 子代理的模型参数独立于 ~/.codex/config.toml（桌面聊天可能开着 Fast，子代理走标准档）
DEFAULT_SANDBOX = os.environ.get("CODEX_SUB_SANDBOX", "danger-full-access")
DEFAULT_MODEL = os.environ.get("CODEX_SUB_MODEL", "gpt-6.1-sol")
DEFAULT_EFFORT = os.environ.get("CODEX_SUB_EFFORT", "high")
DEFAULT_SERVICE_TIER = os.environ.get("CODEX_SUB_SERVICE_TIER", "default")

# 可选模型：Codex 界面显示名 -> app-server 的 model id；Luna 不支持 ultra
MODELS = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna"]
MODEL_DESC = ("GPT-6.1 Sol = gpt-6.1-sol（默认）、GPT-6 Astra = gpt-6-astra、GPT-6 Sol = gpt-6-sol、GPT-6 Luna = gpt-6-luna。"
              "推理强度都支持 low/medium/high/xhigh/max，前三个还支持 ultra。仅用户明确指定时填")

DEFAULT_WAIT_TIMEOUT = 600
MAX_WAIT_S = 3000
HEARTBEAT_S = 10
RPC_TIMEOUT_S = 60
RESULT_PREVIEW_CHARS = 6000

# 随 initialize 返回，Claude Code 会注入系统提示
INSTRUCTIONS = """codex-sub：把任务派给 Codex 子代理（默认 GPT-6.1 Sol，可指定 GPT-6 Astra / Sol / Luna），在后台并行执行。
何时派：边界清楚、几分钟到几十分钟的独立实现/修复/调研任务；可并行推进的多个子任务；
需要第二双眼睛审自己的改动时用 codex_review。自己一两步就能做完的事不要派。
怎么写 prompt：目标、改动范围、完成标准（要跑什么验证）、返回格式（结论 + 文件:行号，不贴大段代码）；续问只发增量指令。
结果：任务结束后会自动注入本会话；codex_review 的结论先给用户看再决定改不改；任务"完成"不等于验收通过，关键改动自己核对。"""


def log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 进程信息（Windows Toolhelp 快照；hook.py 复用）
# ---------------------------------------------------------------------------
def process_table() -> Dict[int, Tuple[int, str]]:
    """pid -> (ppid, exe 名)。"""
    result: Dict[int, Tuple[int, str]] = {}
    if os.name != "nt":
        return result

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap == ctypes.c_void_p(-1).value:
        return result
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.decode("mbcs", "replace"))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return result


def ancestors(pid: int, table: Dict[int, Tuple[int, str]], limit: int = 8) -> List[int]:
    chain: List[int] = []
    cur = pid
    while len(chain) < limit and cur in table:
        cur = table[cur][0]
        if not cur or cur in chain:
            break
        chain.append(cur)
    return chain


def server_alive(table: Dict[int, Tuple[int, str]], server_pid: Any, claude_pid: Any) -> bool:
    """状态文件所属的 MCP 服务还活着：pid 存在、是 python、父进程就是文件名里的 Claude 进程（防 pid 复用）。"""
    entry = table.get(server_pid)
    return bool(entry) and entry[1].lower().startswith("python") and entry[0] == claude_pid


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# 定位 codex 可执行文件
# ---------------------------------------------------------------------------
def find_codex() -> List[str]:
    """优先桌面应用自带的最新版（能读新版 config.toml），其次 PATH。"""
    override = os.environ.get("CODEX_SUB_BIN")
    if override:
        return [override]
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    candidates = sorted(glob.glob(str(base / "*" / "codex.exe")), key=os.path.getmtime)
    if candidates:
        return [candidates[-1]]
    found = shutil.which("codex")
    if found:
        if found.lower().endswith((".cmd", ".bat")):
            # npm 的 cmd 垫片会让 cmd.exe 参与传参；直接调 node + codex.js
            js = Path(found).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = shutil.which("node")
            if js.exists() and node:
                return [node, str(js)]
        return [found]
    raise RuntimeError("找不到 codex，可用 CODEX_SUB_BIN 指定路径")


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
class Task:
    def __init__(self, task_id: str, prompt: str, cwd: str, detach: bool):
        self.id = task_id
        self.prompt = prompt
        self.cwd = cwd
        self.detach = detach
        self.thread_id = ""
        self.turn_id = ""
        self.status = "running"  # running | completed | interrupted | failed
        self.started_at = time.time()
        self.completed_at: Optional[float] = None
        self.model: Optional[str] = None
        self.branch: Optional[str] = None
        self.worktree: Optional[str] = None
        self.message = ""          # 最终/最新的助手消息
        self.message_stream = ""   # delta 累积，用于进度预览
        self.last_activity = ""
        self.files: List[str] = []
        self.error: Optional[str] = None
        self.reported = False      # 终态结果已通过工具返回给 Claude，hook 不再重复注入
        self.done = threading.Event()

    @property
    def elapsed(self) -> float:
        return (self.completed_at or time.time()) - self.started_at

    def finish(self, status: str, error: Optional[str] = None) -> None:
        if self.done.is_set():
            return
        self.status = status
        self.error = error
        self.completed_at = time.time()
        self.done.set()


def format_task(task: Task, full: bool = False) -> str:
    head = f"[{task.id}] {task.status} | {task.elapsed:.0f}s | thread {task.thread_id or '-'}"
    if task.branch:
        head += f" | branch {task.branch} @ {task.worktree}"
    if task.detach:
        head += " | detached"
    lines = [head, f"  任务: {task.prompt[:120]}"]
    if task.status == "running":
        if task.last_activity:
            lines.append(f"  最近动作: {task.last_activity}")
        preview = (task.message_stream or task.message).strip()
        if preview:
            lines.append(f"  输出预览: …{preview[-300:]}")
        if task.files:
            lines.append(f"  已改动 {len(task.files)} 个文件")
        return "\n".join(lines)
    if task.files:
        lines.append("  改动文件: " + ", ".join(task.files))
    if task.error:
        lines.append(f"  错误: {task.error}")
    msg = task.message.strip() or "（无文本输出）"
    if not full and len(msg) > RESULT_PREVIEW_CHARS:
        msg = msg[:RESULT_PREVIEW_CHARS] + f'\n…（已截断，共 {len(task.message)} 字，codex_status(ids=["{task.id}"], full=true) 看全文）'
    lines.append("--- 结果 ---" if task.status == "completed" else "--- 结束前的输出 ---")
    lines.append(msg)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态文件（给 hook.py 读）
# ---------------------------------------------------------------------------
class State:
    def __init__(self) -> None:
        self.tasks: Dict[str, Task] = {}
        self.claude_pid = os.getppid()
        self.path = STATE_DIR / f"session-{self.claude_pid}.json"
        self.app_server_pid: Optional[int] = None
        self.closing = False
        self._lock = threading.Lock()

    def flush(self) -> None:
        with self._lock:
            if self.closing:
                return
            data = {
                "server_pid": os.getpid(), "app_server_pid": self.app_server_pid, "updated": time.time(),
                "tasks": {t.id: {"id": t.id, "status": t.status, "detach": t.detach,
                                 "reported": t.reported, "text": format_task(t)}
                          for t in list(self.tasks.values())},
            }
            body = json.dumps(data, ensure_ascii=False)
            tmp = self.path.with_suffix(".tmp")
            for attempt in range(5):
                try:
                    tmp.write_text(body, encoding="utf-8")
                    os.replace(tmp, self.path)
                    return
                except PermissionError:  # Windows 上 hook 正在读时 replace 会失败
                    time.sleep(0.05 * (attempt + 1))
            log("状态文件写入失败（持续被占用）")

    def cleanup(self) -> None:
        with self._lock:
            self.closing = True
        for p in (self.path, self.path.with_name(self.path.stem + ".ack.json"), self.path.with_name(self.path.stem + ".lock")):
            try:
                p.unlink()
            except OSError:
                pass


def reap_stale_sessions() -> None:
    """清掉 MCP 服务已死的旧会话文件，顺带杀掉它们留下的 app-server（父进程被强杀时 finally 不会执行）。"""
    table = process_table()
    for path in STATE_DIR.glob("session-*.json"):
        if path.name.endswith(".ack.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            claude_pid = int(path.stem.split("-", 1)[1])
        except ValueError:
            continue
        if server_alive(table, data.get("server_pid"), claude_pid):
            continue
        # Windows 不会给孤儿进程换父进程：父 pid 仍是已死的 MCP 服务，借此排除 pid 被复用成别的进程
        app_pid = data.get("app_server_pid")
        entry = table.get(app_pid)
        if entry and entry[0] == data.get("server_pid") and entry[1].lower().startswith(("codex", "node")):
            log(f"回收孤儿 app-server {app_pid}（会话文件 {path.name}）")
            kill_tree(app_pid)
        for p in (path, path.with_name(path.stem + ".ack.json"), path.with_name(path.stem + ".lock")):
            try:
                p.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# codex app-server 客户端
# ---------------------------------------------------------------------------
# 无人值守：服务端发来的审批/追问一律拒绝，按各方法要求的应答格式
_DECLINE_REPLIES: Dict[str, Dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/requestUserInput": {"answers": {}},
    "item/permissions/requestApproval": {"permissions": {}},
    "item/tool/call": {"contentItems": [], "success": False},
    "applyPatchApproval": {"decision": "abort"},
    "execCommandApproval": {"decision": "abort"},
}


class AppServer:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self._start_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._events: Dict[int, threading.Event] = {}
        self.by_thread: Dict[str, Task] = {}
        self.launch_lock = threading.Lock()  # 开 thread + 起 turn 串行化，防止并发续接同一 thread
        self._stderr = None

    def ensure(self) -> None:
        with self._start_lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._reset("app-server 未运行")
            cmd = find_codex() + ["app-server"]
            log(f"启动 app-server: {cmd}")
            if self._stderr:
                self._stderr.close()
            self._stderr = open(STATE_DIR / "app-server.stderr", "wb")
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr)
            self.proc = proc
            threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
            try:
                self.request("initialize", {
                    "clientInfo": {"name": "codex-sub", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                })
                self._write({"jsonrpc": "2.0", "method": "initialized"})
            except Exception:
                kill_tree(proc.pid)
                self.proc = None
                raise
            STATE.app_server_pid = proc.pid
            STATE.flush()

    def _reset(self, reason: str) -> None:
        """进程没了：唤醒所有在途请求，未完成任务标失败。"""
        with self._lock:
            for rid, ev in self._events.items():
                self._pending[rid] = {"error": {"message": reason}}
                ev.set()
            self._events.clear()
            tasks = list(self.by_thread.values())
            self.by_thread.clear()
        for task in tasks:
            task.finish("failed", reason)

    def _write(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            proc = self.proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise RuntimeError("app-server 未运行")
            proc.stdin.write(data)
            proc.stdin.flush()

    def request(self, method: str, params: Dict[str, Any], timeout: float = RPC_TIMEOUT_S) -> Dict[str, Any]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            ev = self._events[rid] = threading.Event()
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            ok = ev.wait(timeout)
        finally:
            with self._lock:
                self._events.pop(rid, None)
                resp = self._pending.pop(rid, None)
        if not ok or resp is None:
            raise RuntimeError(f"{method} 超时（{timeout}s）")
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error'].get('message', resp['error'])}")
        return resp.get("result") or {}

    def _reader(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
                if "method" not in msg:
                    with self._lock:
                        ev = self._events.get(msg.get("id"))
                        if ev is not None:
                            self._pending[msg["id"]] = msg
                            ev.set()
                elif "id" in msg:
                    reply = _DECLINE_REPLIES.get(msg["method"])
                    if reply is not None:
                        self._write({"jsonrpc": "2.0", "id": msg["id"], "result": reply})
                    else:
                        self._write({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601, "message": "codex-sub 不处理此请求"}})
                else:
                    self._on_notification(msg["method"], msg.get("params") or {})
            except Exception as exc:  # 单条消息出错不能拖垮读循环
                log(f"读循环异常: {exc!r}")
        if self.proc is proc:
            tail = _stderr_tail()
            log(f"app-server 退出: {tail[-200:]}")
            self._reset("app-server 进程退出" + (f"：{tail[-500:]}" if tail else ""))
            STATE.flush()

    def _on_notification(self, method: str, p: Dict[str, Any]) -> None:
        task = self.by_thread.get(p.get("threadId", ""))
        if task is None:
            return
        turn_id = p.get("turnId") or (p.get("turn") or {}).get("id")
        if task.turn_id and turn_id and turn_id != task.turn_id:
            return  # 同一 thread 上旧 turn 迟到的事件
        if method == "turn/started":
            task.turn_id = task.turn_id or turn_id or ""
        elif method == "item/started":
            item = p.get("item") or {}
            kind = item.get("type")
            if kind == "commandExecution":
                task.last_activity = "$ " + str(item.get("command", ""))[:200]
            elif kind == "fileChange":
                task.last_activity = "编辑 " + ", ".join(c.get("path", "") for c in item.get("changes", []))[:200]
            elif kind == "mcpToolCall":
                task.last_activity = f"MCP {item.get('server')}/{item.get('tool')}"
            elif kind == "webSearch":
                task.last_activity = "搜索 " + str(item.get("query", ""))[:120]
        elif method == "item/agentMessage/delta":
            task.message_stream += p.get("delta", "")
        elif method == "item/completed":
            item = p.get("item") or {}
            kind = item.get("type")
            if kind == "agentMessage":
                task.message = item.get("text") or task.message_stream
                task.message_stream = ""
            elif kind == "exitedReviewMode" and item.get("review"):
                task.message = item["review"]
            elif kind == "fileChange":
                for c in item.get("changes", []):
                    path = c.get("path")
                    if path and path not in task.files:
                        task.files.append(path)
        elif method == "thread/status/changed":
            flags = (p.get("status") or {}).get("activeFlags") or []
            if flags:
                task.last_activity = "等待审批/输入（已自动拒绝）: " + ", ".join(flags)
        elif method == "error":
            msg = (p.get("error") or {}).get("message", "")
            task.last_activity = ("重试中: " if p.get("willRetry") else "错误: ") + msg[:200]
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            if not task.message and task.message_stream:
                task.message = task.message_stream
            err = turn.get("error") or {}
            error = None
            if err:
                error = err.get("message", "")
                if err.get("codexErrorInfo"):
                    error = f"[{err['codexErrorInfo']}] {error}"
            task.finish(turn.get("status", "completed"), error)
            STATE.flush()


def _stderr_tail() -> str:
    try:
        with open(STATE_DIR / "app-server.stderr", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 2000))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


APP = AppServer()
STATE = State()


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
def _create_worktree(task_id: str, cwd: str) -> Dict[str, str]:
    run = lambda *a: subprocess.run(
        ["git", "-C", cwd, *a], stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise RuntimeError(f"worktree 需要 git 仓库: {top.stderr.strip()}")
    root = Path(top.stdout.strip())
    sub = Path(cwd).resolve().relative_to(root.resolve())
    branch = f"codex-sub/{task_id}"
    wt = root.parent / f"{root.name}.codex-sub" / task_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = run("worktree", "add", "-b", branch, str(wt), "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add 失败: {r.stderr.strip()}")
    return {"branch": branch, "worktree": str(wt), "cwd": str(wt / sub)}


def _remove_worktree(task: Task, repo_cwd: str) -> None:
    """从原仓库目录执行；Windows 下在待删目录内跑 git 会让目录删不掉。"""
    if not task.worktree:
        return
    for args in (["worktree", "remove", "--force", task.worktree], ["branch", "-D", task.branch or ""]):
        subprocess.run(["git", "-C", repo_cwd, *args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _open_thread(task: Task, a: Dict[str, Any], sandbox: str) -> None:
    """新建 thread 或续接旧 thread，填好 task.thread_id / task.model，并登记到 by_thread。"""
    thread_id = a.get("thread_id")
    base = {"cwd": task.cwd, "sandbox": sandbox, "approvalPolicy": "never",
            "model": a.get("model") or DEFAULT_MODEL, "serviceTier": DEFAULT_SERVICE_TIER}
    if thread_id:
        prev = APP.by_thread.get(thread_id)
        if prev is not None:
            if not prev.done.is_set():
                raise ValueError(f"thread {thread_id} 上的任务 {prev.id} 还在运行，等它结束再续问，或先 codex_interrupt")
            # 本进程派出的 thread 仍在 app-server 内存里，直接开新 turn（临时会话也行）
            task.thread_id, task.model = thread_id, prev.model
        else:
            # 上次会话持久化的 thread，需要从磁盘恢复
            try:
                res = APP.request("thread/resume", dict(base, threadId=thread_id))
            except RuntimeError as exc:
                raise RuntimeError(f"无法续问 {thread_id}：{exc}。临时会话只在本会话的 app-server 存活期间可续问，"
                                   "需要跨会话续问的任务请用 persist=true 派发") from exc
            task.thread_id, task.model = str(res["thread"]["id"]), res.get("model")
    else:
        # 默认临时会话：不写入 ~/.codex，也不会出现在 Codex 应用的历史里
        res = APP.request("thread/start", dict(base, ephemeral=not a.get("persist")))
        task.thread_id, task.model = str(res["thread"]["id"]), res.get("model")
        if a.get("persist"):
            try:
                APP.request("thread/name/set", {"threadId": task.thread_id, "name": f"codex-sub {task.id}: {task.prompt[:40]}"})
            except RuntimeError:
                pass
    APP.by_thread[task.thread_id] = task


def _launch(a: Dict[str, Any], prompt: str, sandbox: str, start_turn, progress: Any) -> str:
    """spawn / review 的公共流程：建任务 -> 开 thread -> 起 turn -> 登记；任一步失败都不留半成品。"""
    cwd = os.path.abspath(a.get("cwd") or os.getcwd())
    if not os.path.isdir(cwd):
        raise ValueError(f"cwd 不存在: {cwd}")
    if a.get("worktree") and a.get("thread_id"):
        raise ValueError("worktree 不能和 thread_id 同时使用")
    task = Task(uuid.uuid4().hex[:8], prompt, cwd, bool(a.get("detach")))
    try:
        if a.get("worktree"):
            info = _create_worktree(task.id, cwd)
            task.branch, task.worktree, task.cwd = info["branch"], info["worktree"], info["cwd"]
        APP.ensure()
        with APP.launch_lock:
            _open_thread(task, a, sandbox)
            try:
                task.turn_id = start_turn(task)
            except Exception:
                if APP.by_thread.get(task.thread_id) is task:
                    del APP.by_thread[task.thread_id]
                raise
    except Exception:
        _remove_worktree(task, cwd)
        raise
    STATE.tasks[task.id] = task
    STATE.flush()

    wait_s = min(float(a.get("wait_s") or 0), MAX_WAIT_S)
    if wait_s > 0:
        _wait([task], wait_s, progress)
    return _report(task)


def _report(task: Task, full: bool = False) -> str:
    """终态结果一旦通过工具返回，就标记已送达，hook 不再重复注入。"""
    if task.done.is_set() and not task.reported:
        task.reported = True
        STATE.flush()
    return format_task(task, full)


def tool_spawn(a: Dict[str, Any], progress: Any = None) -> str:
    prompt = (a.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt 不能为空")
    effort = a.get("effort") or DEFAULT_EFFORT

    def start(task: Task) -> str:
        res = APP.request("turn/start", {"threadId": task.thread_id, "effort": effort,
                                         "input": [{"type": "text", "text": prompt}]})
        return res["turn"]["id"]

    return _launch(a, prompt, DEFAULT_SANDBOX, start, progress)


def tool_review(a: Dict[str, Any], progress: Any = None) -> str:
    """Codex 内置代码审查模式（等价于 codex review）：找缺陷，不改文件。"""
    scope = a.get("scope") or "uncommitted"
    value = a.get("value") or ""
    targets = {
        "uncommitted": lambda: {"type": "uncommittedChanges"},
        "base_branch": lambda: {"type": "baseBranch", "branch": value},
        "commit": lambda: {"type": "commit", "sha": value},
        "custom": lambda: {"type": "custom", "instructions": value},
    }
    if scope not in targets:
        raise ValueError(f"未知 scope: {scope}")
    if scope != "uncommitted" and not value:
        raise ValueError(f"scope={scope} 需要 value")

    def start(task: Task) -> str:
        res = APP.request("review/start", {"threadId": task.thread_id, "target": targets[scope](), "delivery": "inline"})
        return res["turn"]["id"]

    return _launch(dict(a, thread_id=None), f"[review:{scope}] {value}".strip(), DEFAULT_SANDBOX, start, progress)


def _wait(tasks: List[Task], timeout: float, progress: Any, mode: str = "all") -> None:
    deadline = time.time() + timeout
    last_beat = time.time()
    while True:
        pending = [t for t in tasks if not t.done.is_set()]
        if not pending or (mode == "any" and len(pending) < len(tasks)) or time.time() >= deadline:
            return
        pending[0].done.wait(min(2.0, max(0.1, deadline - time.time())))
        if progress and time.time() - last_beat >= HEARTBEAT_S:
            last_beat = time.time()
            progress(f"等待 {len(pending)} 个 Codex 任务: " + "; ".join(
                f"{t.id} {t.last_activity or t.status}"[:80] for t in pending))


def _get(task_id: str) -> Task:
    task = STATE.tasks.get(task_id)
    if task is None:
        raise ValueError(f"没有任务 {task_id}")
    return task


def tool_wait(a: Dict[str, Any], progress: Any = None) -> str:
    ids = a.get("ids") or [t.id for t in STATE.tasks.values() if not t.done.is_set()]
    if not ids:
        return "没有需要等待的任务"
    tasks = [_get(i) for i in ids]
    _wait(tasks, min(float(a.get("timeout") or DEFAULT_WAIT_TIMEOUT), MAX_WAIT_S), progress, a.get("mode") or "all")
    out = [_report(t) for t in tasks]
    still = [t.id for t in tasks if not t.done.is_set()]
    if still:
        out.append(f"注意：{', '.join(still)} 仍在运行（等待超时不是失败），可再次 codex_wait 或 codex_status 查看。")
    return "\n\n".join(out)


def tool_status(a: Dict[str, Any], progress: Any = None) -> str:
    ids = a.get("ids")
    if not ids:
        if not STATE.tasks:
            return "当前没有任务"
        return "\n".join(
            f"[{t.id}] {t.status:<11} {t.elapsed:5.0f}s  {t.prompt[:70]!r}" + ("  (detached)" if t.detach else "")
            for t in STATE.tasks.values())
    return "\n\n".join(_report(_get(i), bool(a.get("full"))) for i in ids)


def tool_steer(a: Dict[str, Any], progress: Any = None) -> str:
    task = _get(a["id"])
    if task.done.is_set():
        raise ValueError(f"任务 {task.id} 已结束（{task.status}），续问请用 codex_spawn(thread_id=...)")
    APP.request("turn/steer", {"threadId": task.thread_id, "expectedTurnId": task.turn_id,
                               "input": [{"type": "text", "text": a["prompt"]}]})
    task.last_activity = "已收到新指令"
    return f"已把新指令送入任务 {task.id}（Codex 会在当前步骤后转向）"


def tool_interrupt(a: Dict[str, Any], progress: Any = None) -> str:
    task = _get(a["id"])
    if not task.done.is_set():
        APP.request("turn/interrupt", {"threadId": task.thread_id, "turnId": task.turn_id})
        task.done.wait(15)
    return _report(task)


_SPAWN_SCHEMA = {
    "type": "object",
    "required": ["prompt"],
    "properties": {
        "prompt": {"type": "string", "description": "任务说明：目标、改动范围、完成标准（要跑什么验证）、返回格式（结论 + 文件:行号，不贴大段代码）"},
        "cwd": {"type": "string", "description": "工作目录（绝对路径），默认当前目录"},
        "wait_s": {"type": "number", "description": "先同步等待这么多秒；估计几分钟内能完成的任务填 120~300 可直接拿到结果，长任务填 0 立即返回"},
        "thread_id": {"type": "string", "description": "续问：在之前某个任务的 thread 上继续，保留全部上下文，只发增量指令"},
        "persist": {"type": "boolean", "description": "把会话持久化到 Codex 历史，以便下次 Claude 会话还能用 thread_id 续问；默认不持久化"},
        "model": {"type": "string", "enum": MODELS, "description": MODEL_DESC},
        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "ultra"], "description": "推理强度，默认 high；简单任务可降到 low/medium"},
        "worktree": {"type": "boolean", "description": "在独立 git worktree（分支 codex-sub/<id>）里执行，多任务并行改同一仓库时用，完成后自行合并分支"},
        "detach": {"type": "boolean", "description": "true = 不关心结果的后台任务；Claude 结束回合时不会因为它被拦下"},
    },
}

TOOLS = [
    {"name": "codex_spawn",
     "description": (
         "把一个任务派给 Codex 子代理，立即返回 8 位任务 id（wait_s>0 时先等一会儿，等到就直接给结果）。"
         "可以连续派多个，它们在同一个常驻 Codex 进程里并行执行。任务完成后结果会自动送回本会话；"
         "也可以用 codex_wait 主动等、codex_status 看进度、codex_steer 中途改方向、codex_interrupt 中断。"
         "传 thread_id 可在旧任务的上下文上续问；该 thread 上不能有仍在运行的任务。"
         "默认无沙箱、不审批：Codex 会直接改文件、跑命令。"),
     "inputSchema": _SPAWN_SCHEMA,
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}},
    {"name": "codex_review",
     "description": (
         "用 Codex 内置的代码审查模式（等价于 `codex review`）审查改动，审查模式本身不改文件、以找缺陷为主，"
         "返回按严重度排序、带文件行号的问题列表。scope=uncommitted 审工作区未提交改动；"
         "base_branch 审当前分支相对基准分支的改动（value=分支名）；commit 审某个提交（value=sha）；"
         "custom 按自定义说明审（value=说明）。异步/等待语义同 codex_spawn，也返回任务 id。"
         "想让 Codex 改代码用 codex_spawn，只想让它挑毛病用这个。"),
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string", "enum": ["uncommitted", "base_branch", "commit", "custom"], "description": "审查范围，默认 uncommitted"},
         "value": {"type": "string", "description": "scope 对应的值：分支名 / commit sha / 自定义审查说明；uncommitted 不需要"},
         "cwd": {"type": "string", "description": "git 仓库目录（绝对路径），默认当前目录"},
         "wait_s": {"type": "number", "description": "先同步等待的秒数，审查通常 1~3 分钟；0 立即返回"},
         "model": {"type": "string", "enum": MODELS, "description": MODEL_DESC},
         "detach": {"type": "boolean", "description": "true = 不关心结果，Claude 结束回合时不等它"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}},
    {"name": "codex_wait",
     "description": (
         "阻塞等待一个或多个任务结束并返回它们的结果。不传 ids 就等本会话全部运行中的任务。"
         "超时只是返回当前状态，任务不会被杀，可以再次调用继续等。已结束的任务立即返回。"
         "等待期间每 10 秒发一次进度通知。只想看一眼不想等，用 codex_status。"),
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}, "description": "任务 id 列表（codex_spawn / codex_review 返回的 8 位 id）；省略 = 全部运行中的任务"},
         "timeout": {"type": "number", "description": f"最长等待秒数，默认 {DEFAULT_WAIT_TIMEOUT}，上限 {MAX_WAIT_S}"},
         "mode": {"type": "string", "enum": ["all", "any"], "description": "all=全部结束才返回（默认），any=任一结束就返回"}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "codex_status",
     "description": (
         "立即返回，不等待。不传 ids：一行一个列出本会话全部任务及状态。传 ids：看详情——"
         "运行中的显示最近动作、改动文件、输出预览；已结束的显示完整结果（超过 6000 字截断，full=true 看全文）。"
         "被 codex_interrupt 中断的任务，其已产出的部分也从这里取。"),
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}, "description": "任务 id 列表；省略 = 只列清单"},
         "full": {"type": "boolean", "description": "true = 不截断，返回完整结果文本"}}},
     "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "codex_steer",
     "description": (
         "给一个正在运行的任务追加或修正指令，Codex 在当前步骤结束后转向，不用重来，已做的工作保留。"
         "只对运行中的任务有效；任务已结束时报错，此时应改用 codex_spawn(thread_id=...) 续问。"
         "想让它停下来而不是转向，用 codex_interrupt。"),
     "inputSchema": {"type": "object", "required": ["id", "prompt"], "properties": {
         "id": {"type": "string", "description": "运行中任务的 8 位 id（codex_spawn 返回的）"},
         "prompt": {"type": "string", "description": "追加的指令，只写增量，不用重复原任务"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}},
    {"name": "codex_interrupt",
     "description": (
         "中断一个正在运行的任务：Codex 收到后立即停止，已完成的部分输出和文件改动保留，任务状态变为 interrupted，"
         "结果可用 codex_status 查看。对已结束的任务调用无副作用，直接返回其结果。"
         "被中断的 thread 之后仍可用 codex_spawn(thread_id=...) 续问。只想改方向不想停，用 codex_steer。"),
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string", "description": "任务的 8 位 id（codex_spawn / codex_review 返回的）"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
]

HANDLERS = {
    "codex_spawn": tool_spawn, "codex_review": tool_review, "codex_wait": tool_wait,
    "codex_status": tool_status, "codex_steer": tool_steer, "codex_interrupt": tool_interrupt,
}


# ---------------------------------------------------------------------------
# MCP stdio 服务
# ---------------------------------------------------------------------------
_out_lock = threading.Lock()


def send(obj: Dict[str, Any]) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def handle(req: Dict[str, Any]) -> None:
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex-sub", "version": __version__},
            "instructions": INSTRUCTIONS}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = str(params.get("name"))
        token = (params.get("_meta") or {}).get("progressToken")
        progress: Any = None
        if token is not None:
            progress = lambda text: send({"jsonrpc": "2.0", "method": "notifications/progress",
                                          "params": {"progressToken": token, "progress": time.time(), "message": text}})
        fn = HANDLERS.get(name)
        if fn is None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"未知工具 {name}"}})
            return
        try:
            text = fn(params.get("arguments") or {}, progress)
            send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}]}})
        except Exception as exc:
            log(f"{name} 失败: {exc!r}")
            send({"jsonrpc": "2.0", "id": rid, "result": {"isError": True,
                  "content": [{"type": "text", "text": f"{name} 失败: {exc}"}]}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"不支持 {method}"}})


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows 默认 GBK
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    reap_stale_sessions()
    log(f"codex-sub {__version__} 启动，claude pid {STATE.claude_pid}")
    STATE.flush()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if req.get("method") == "tools/call":
                threading.Thread(target=handle, args=(req,), daemon=True).start()
            else:
                handle(req)
    finally:
        STATE.cleanup()
        if APP.proc and APP.proc.poll() is None:
            kill_tree(APP.proc.pid)
        log("退出")


if __name__ == "__main__":
    main()
