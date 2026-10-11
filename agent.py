#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent.py —— 薄内核参考实现（配套《Skill 最小规范》v0.2）

单文件、零依赖、Python 3.6+ 语法地板（勿用 walrus / match / dataclass / text= 参数）。
设计出处：
  循环与执行语义  借鉴 mini-SWE-agent（无状态子进程动作 / 协议可换解析 / 预算计数器 / 显式终止）
  部署与交互面    借鉴 EVA（配置区+环境变量 / 目录级会话 / 读写二分默认策略 / Ctrl+C 语义 / CLI 面）
  上下文机制      借鉴 Pi（AGENT.md 逐层加载 / 压缩即可替换 prompt / memory 文件约定）
  审计纪律        借鉴 Cordis（单一执行闸门 / append-only 事件日志唯一事实源 / 纯函数投影）

用法：
  交互式    python3 agent.py                 # 自动延续本目录（按 pwd+用户名）的会话
  一次性    python3 agent.py -u '看看这台机器状态如何'
  带会话    python3 agent.py -su '接着上次继续'
  目标模式  python3 agent.py -agu '把 /var/log 下最近的错误汇总成报告'
  会话管理  python3 agent.py -l              # 列出本目录会话
            python3 agent.py -c              # 清空本目录会话
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ================= LLM 配置区（环境变量优先，此处兜底） =================
# 内网部署：指向 vLLM / SGLang 的 OpenAI 兼容端点，例如 http://10.0.0.8:8000/v1
AGENT_BASE_URL = os.environ.get("AGENT_BASE_URL") or ""   # 例: "http://10.0.0.8:8000/v1"
AGENT_API_KEY = os.environ.get("AGENT_API_KEY") or ""     # 例: "sk-xxx"（无鉴权可留空）
AGENT_MODEL = os.environ.get("AGENT_MODEL") or ""         # 例: "Qwen3-32B"
# ========================================================================

# ---- 预算护栏（均为硬约束，并作为软提示注入上下文）----
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "40"))        # 每轮/每目标最大模型调用数
MAX_SECONDS = int(os.environ.get("AGENT_MAX_SECONDS", "1800"))  # 最大墙钟时长
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "200000"))  # 累计 token 上限（端点返回 usage 才生效）
COMPACT_CHARS = int(os.environ.get("AGENT_COMPACT_CHARS", "60000"))  # 触发压缩的消息体积（字符）
KEEP_TAIL = 6                                                   # 压缩时保留的最近消息数

OUT_TRUNC = int(os.environ.get("AGENT_OUT_TRUNC", "8000"))      # 单次工具输出截断（字符）
HTTP_TIMEOUT = int(os.environ.get("AGENT_HTTP_TIMEOUT", "300")) # LLM 请求超时（秒）
CMD_TIMEOUT = int(os.environ.get("AGENT_CMD_TIMEOUT", "120"))   # 单条命令默认超时（秒）
DOC_TRUNC = 4000                                                # AGENT.md / memory.md 注入截断

CFG_DIR = ".agent"          # 项目内状态目录（sessions / audit.jsonl / memory.md / compact.md）
SESSION_MODE_EPHEMERAL = "ephemeral"

# ---- 安全闸门策略（按公司安全基线调整这两张表即可）----
# 纯读命令白名单：整条命令不含 shell 元字符且首词命中，自动放行
READ_BINS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "find", "wc", "file",
    "stat", "df", "du", "free", "ps", "uptime", "uname", "pwd", "date", "whoami",
    "hostname", "sort", "uniq", "ss", "ip", "id", "which", "echo", "printf",
    "nproc", "lscpu", "lsblk", "vmstat", "iostat", "last", "w", "env",
}
# 硬拒绝：即便 -a 也不放行（正则，匹配整条命令文本）
HARD_DENY = [
    r"\brm\s+(-\w+\s+)*-\w*[rf]\w*\s+(/\*|/|~|\$HOME)(\s|$)",  # rm -rf 根/家目录
    r"\b(mkfs|fdisk|mkswap)\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r":\(\)\s*\{",                                             # fork 炸弹
    r"\b(shutdown|poweroff|reboot|halt)\b",
    r"\binit\s+[06]\b",
]
SHELL_META = re.compile(r"[;|&<>`$\\]|\(|\)|\n")               # 含元字符则不算"纯读命令"

# ---- 文本协议（模型不支持 tool calling 时的降级格式）----
BASH_BLOCK = re.compile(r"```(?:bash|sh)\s*\n(.*?)```", re.S)
READ_BLOCK = re.compile(r"```read\s*\n(.*?)```", re.S)

TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "在 Linux bash 中执行一条命令。命令无状态（cd、export 不跨步保留），路径一律用绝对路径。写命令需用户确认。输出会被截断，大结果请写入文件后只返回路径与摘要。",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"},
                                      "timeout": {"type": "integer", "description": "秒，默认 %d" % CMD_TIMEOUT}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读取文本文件内容（用于阅读 skill 的 SKILL.md、references 或项目文件）。大文件会被截断。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
]

DEFAULT_COMPACT_PROMPT = """\
你是压缩器。把下面的对话历史压缩成一份供后续继续工作使用的摘要，要求：
1. 保留：用户目标与约束、已完成的操作及其结果、关键文件路径、未解决的问题、下一步计划；
2. 丢弃：寒暄、重复探索、已被取代的中间结论；
3. 输出纯文本摘要，不超过 800 字。

对话历史：
"""


# ==================== 基础设施 ====================

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


USER = getpass.getuser()
CWD = Path.cwd()


def cfg_dir():
    d = CWD / CFG_DIR
    d.mkdir(exist_ok=True)
    return d


def audit(action, **kw):
    """全局审计日志：每个闸门决策一行 JSON，供安全团队回溯。"""
    rec = {"ts": now_iso(), "user": USER, "cwd": str(CWD), "action": action}
    rec.update(kw)
    try:
        with open(str(cfg_dir() / "audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def c(text, code):  # 终端着色（tty 才上色，零依赖）
    if sys.stdout.isatty():
        return "\033[%sm%s\033[0m" % (code, text)
    return text


# ==================== 会话：append-only 事件日志 + 纯函数投影 ====================

class Session(object):
    """事件是唯一事实源；喂给模型的消息由 project() 从事件链投影得出。"""

    def __init__(self, persist=True):
        self.persist = persist
        self.events = []
        if persist:
            d = cfg_dir() / "sessions"
            d.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256((str(CWD.resolve()) + "|" + USER).encode("utf-8")).hexdigest()[:16]
            self.path = d / (h + ".jsonl")
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            self.events.append(json.loads(line))
                        except ValueError:
                            pass  # 坏行不致命，投影时跳过
        else:
            self.path = None

    def head(self):
        return self.events[-1]["id"] if self.events else None

    def append(self, etype, message=None, **kw):
        ev = {"id": "e%d" % (len(self.events) + 1), "parent": self.head(),
              "ts": now_iso(), "type": etype}
        if message is not None:
            ev["message"] = message
        ev.update(kw)
        self.events.append(ev)
        if self.persist:
            try:
                with open(str(self.path), "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except OSError as e:
                print(c("警告：会话写入失败 %s" % e, "33"), file=sys.stderr)
        return ev

    def project(self):
        """纯函数投影：从 head 沿 parent 回溯，遇压缩/清空边界停止。"""
        by_id = {e["id"]: e for e in self.events}
        chain, cur = [], self.head()
        while cur and cur in by_id:
            ev = by_id[cur]
            chain.append(ev)
            if ev["type"] in ("compact", "clear"):  # 边界：更早的历史被取代
                break
            cur = ev.get("parent")
        chain.reverse()
        return [e["message"] for e in chain
                if e["type"] in ("user", "assistant", "tool", "compact") and e.get("message")]


# ==================== 安全闸门（唯一执行通路，不可绕过） ====================

def classify(cmd):
    """返回 deny / allow / ask。deny 优先于一切；纯读命令直通；其余需确认。"""
    for pat in HARD_DENY:
        if re.search(pat, cmd):
            return "deny"
    if not SHELL_META.search(cmd):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return "ask"
        if parts and os.path.basename(parts[0]) in READ_BINS:
            return "allow"
    return "ask"


def confirm(cmd):
    print(c("命令需要确认：", "33") + c(cmd, "1"))
    try:
        r = input(c("  [y] 执行一次 / [n] 拒绝 / [a] 本会话不再询问：", "36")).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return "n"
    return r or "n"


def run_bash(cmd, timeout, state):
    """所有命令的唯一入口：拦截虚拟命令 → 闸门 → 执行 → 审计。"""
    cmd = cmd.strip()
    if cmd.startswith("agent-done"):  # 虚拟命令：目标模式终止哨兵，不进 shell
        return "DONE", cmd[len("agent-done"):].strip(" ：:") or "目标达成"

    level = classify(cmd)
    if level == "deny":
        audit("gate", cmd=cmd, decision="deny")
        return "命令被安全策略硬拒绝（命中危险模式），未执行。", None
    if level == "ask" and not state["allow_all"]:
        if not sys.stdin.isatty():
            audit("gate", cmd=cmd, decision="deny-noninteractive")
            return "非交互模式且未加 -a，需确认的命令被拒绝。可加 -a 或改用纯读命令。", None
        r = confirm(cmd)
        audit("gate", cmd=cmd, decision="confirm-" + r)
        if r == "a":
            state["allow_all"] = True
        elif r != "y":
            return "用户拒绝了该命令。请换一种方式或向用户解释后再试。", None
    else:
        audit("gate", cmd=cmd, decision="allow" if level == "allow" else "allow-all")

    t0 = time.time()
    try:
        p = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True,
                           timeout=timeout)
        out = p.stdout or ""
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, code = "(超时，已终止，>%ds)" % timeout, 124
    except KeyboardInterrupt:
        audit("exec", cmd=cmd, exit="interrupted")
        raise
    audit("exec", cmd=cmd, exit=code, ms=int((time.time() - t0) * 1000))
    out = out.strip() or "(无输出，退出码 %d)" % code
    if len(out) > OUT_TRUNC:
        out = out[:OUT_TRUNC] + "\n...(已截断，共 %d 字符，完整结果请落盘后再读)" % len(out)
    return "退出码 %d\n%s" % (code, out), None


def run_read_file(path, skills):
    p = Path(os.path.expanduser(path))
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return "读取失败：%s" % e
    for s in skills:  # {SKILL_DIR} 占位符注入（规范 §2.2）
        try:
            p.resolve().relative_to(Path(s["dir"]).resolve())
            text = text.replace("{SKILL_DIR}", str(Path(s["dir"]).resolve()))
            break
        except ValueError:
            continue
    if len(text) > OUT_TRUNC:
        text = text[:OUT_TRUNC] + "\n...(已截断)"
    return text


# ==================== 技能加载（规范 §2 / §3 / §5） ====================

def parse_frontmatter(text):
    """极简 YAML 子集解析：仅支持 `key: value`、嵌套一层、`[a, b]` 行内列表。"""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm, cur_key = {}, None
    for raw in text[3:end].strip().splitlines():
        if not raw.strip():
            continue
        indented = raw[0] in " \t"
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            val = [x.strip().strip("'\"") for x in val[1:-1].split(",") if x.strip()]
        elif len(val) > 1 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        if not indented:
            if val == "":
                fm[key] = {}
                cur_key = key
            else:
                fm[key] = val
                cur_key = None
        elif cur_key:
            fm[cur_key][key] = val
    fm["__body__"] = text[end + 4:]
    return fm


def version_ok(req):
    m = re.match(r"(>=|<=|==|>|<)?\s*([\d.]+)", str(req))
    if not m:
        return True
    op, want = m.group(1) or ">=", tuple(int(x) for x in m.group(2).split("."))
    cur = sys.version_info[:len(want)]
    return {"<": cur < want, "<=": cur <= want, ">": cur > want,
            "==": cur == want}.get(op, cur >= want)


def probe(req):
    """requires 探测：返回 None 表示可用，否则返回失败原因（规范 §3）。"""
    if not isinstance(req, dict):
        return None
    for b in req.get("bins") or []:
        if not shutil.which(str(b)):
            return "缺少命令: %s" % b
    if req.get("python") and not version_ok(req["python"]):
        return "python 版本不满足: %s（当前 %s）" % (req["python"], sys.version.split()[0])
    for e in req.get("env") or []:
        if not os.environ.get(str(e)):
            return "缺少环境变量: %s" % e
    return None


def load_skills():
    """扫描 → 探测 → 索引。探测失败的 skill 不进索引、模型不可见，仅记日志。"""
    dirs = []
    for env in os.environ.get("AGENT_SKILLS_DIR", "").split(os.pathsep):
        if env.strip():
            dirs.append(Path(env.strip()))
    dirs += [CWD / "skills", Path(__file__).resolve().parent / "skills",
             Path.home() / CFG_DIR / "skills"]
    skills, seen = [], set()
    for base in dirs:
        if not base.is_dir():
            continue
        for sk in sorted(base.iterdir()):
            md = sk / "SKILL.md"
            if not sk.is_dir() or not md.is_file() or sk.name in seen:
                continue
            if not re.match(r"^[a-z0-9][a-z0-9-]*$", sk.name):
                audit("skill", name=sk.name, decision="invalid-name")
                continue
            try:
                fm = parse_frontmatter(md.read_text(encoding="utf-8"))
            except OSError:
                fm = None
            if not fm or fm.get("name") != sk.name or not fm.get("description"):
                audit("skill", name=sk.name, decision="invalid-frontmatter")
                continue
            seen.add(sk.name)
            reason = probe(fm.get("requires"))
            if reason:
                audit("skill", name=sk.name, decision="filtered", reason=reason)
                continue
            skills.append({"name": sk.name, "description": str(fm["description"]),
                           "dir": str(sk)})
            audit("skill", name=sk.name, decision="loaded")
    return skills


# ==================== 上下文装配（Pi 式分层指令 + memory 约定） ====================

def layered_docs():
    """~/.agent/AGENT.md 全局层 → 自根到 cwd 的 AGENT.md/AGENTS.md 项目层。"""
    docs = []
    g = Path.home() / CFG_DIR / "AGENT.md"
    if g.is_file():
        docs.append(("全局设定 " + str(g), g))
    chain = list(reversed([CWD] + list(CWD.parents)))
    for d in chain:
        for name in ("AGENT.md", "AGENTS.md"):
            p = d / name
            if p.is_file():
                docs.append((str(p), p))
                break
    return docs


def build_system(skills, goal_mode, no_tools):
    parts = [
        "你是一个 Linux 运维与工程助手，运行在企业内网的受限环境中。工作原则：",
        "1. 先读后写，先验证后下结论；危险操作前先向用户解释。",
        "2. shell 无状态：cd、export 不跨命令保留，一律使用绝对路径。",
        "3. 上下文经济：大输出写入文件后只返回路径与摘要；不要重复读取已读过的全文。",
        "4. 工具结果尾部附有预算状态行（step/elapsed/tokens），请自行控制节奏，接近上限时收敛任务。",
    ]
    if no_tools:
        parts += [
            "5. 当前端点不支持工具调用，改用文本协议：要执行命令输出 ```bash 代码块（一块一条命令）；"
            "要读文件输出 ```read 代码块（内容一行，为文件路径）。每轮最多各一块，结果会以用户消息返回。",
        ]
    if skills:
        parts.append("\n可用技能（按需用 read_file 阅读对应 SKILL.md 全文后再使用）：")
        parts += ["- %s: %s" % (s["name"], s["description"]) for s in skills]
    for title, p in layered_docs():
        try:
            t = p.read_text(encoding="utf-8", errors="replace")[:DOC_TRUNC]
        except OSError:
            continue
        parts.append("\n【项目指令 %s】\n%s" % (title, t))
    mem = cfg_dir() / "memory.md"
    if mem.is_file():
        t = mem.read_text(encoding="utf-8", errors="replace")[:DOC_TRUNC]
        parts.append("\n【持久记忆 .agent/memory.md（可由你更新，写入需过闸门）】\n%s" % t)
    if goal_mode:
        parts.append("\n目标模式：自主推进直到目标达成。达成后执行命令 `agent-done <一句话总结>` 结束"
                     "（该命令由内核拦截，不会真正运行）。未达成则持续行动，不要空谈。")
    return {"role": "system", "content": "\n".join(parts)}


# ==================== LLM 调用（OpenAI 兼容，协议可换，对内网网关宽容） ====================

def llm_call(messages, state):
    """返回 (assistant_message, usage)。工具模式遇端点不支持时自动降级文本模式。"""
    body = {"model": AGENT_MODEL, "messages": messages}
    if not state["no_tools"]:
        body["tools"] = TOOLS
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        AGENT_BASE_URL.rstrip("/") + "/chat/completions", data=data,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + (AGENT_API_KEY or "EMPTY")})
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 400 and not state["no_tools"] and "tool" in detail.lower():
                state["no_tools"] = True          # 端点/模型不支持 tools：降级并重建请求
                audit("llm", event="fallback-to-text-mode", detail=detail[:200])
                return llm_call(messages, state)
            raise RuntimeError("LLM HTTP %s: %s" % (e.code, detail))
        except urllib.error.URLError as e:
            if attempt == 0:
                time.sleep(2)
                continue
            raise RuntimeError("LLM 连接失败：%s" % e)
    # 宽容解析：合并所有 choice，优先取带内容的（兼容把文本/tool_calls 拆开的网关）
    msg, usage = {}, payload.get("usage") or {}
    for ch in payload.get("choices") or []:
        m = ch.get("message") or {}
        for k in ("content", "tool_calls"):
            if m.get(k) and not msg.get(k):
                msg[k] = m[k]
    msg["role"] = "assistant"
    return msg, usage


def extract_calls(msg):
    """协议可换解析层：优先 tool_calls；否则从正文提取 ```bash / ```read 代码块。"""
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = (tc.get("function") or {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append((tc.get("id") or "call", fn.get("name"), args))
    if calls:
        return calls
    content = msg.get("content") or ""
    for block in BASH_BLOCK.findall(content)[:1]:
        return [("text", "bash", {"command": block.strip()})]
    for block in READ_BLOCK.findall(content)[:1]:
        return [("text", "read_file", {"path": block.strip().splitlines()[0]})]
    return []


# ==================== 预算与压缩 ====================

class Budget(object):
    def __init__(self):
        self.steps, self.tokens, self.t0 = 0, 0, time.time()

    def line(self):
        return "\n[budget] step %d/%d · elapsed %ds/%ds · tokens %d/%d" % (
            self.steps, MAX_STEPS, int(time.time() - self.t0), MAX_SECONDS,
            self.tokens, MAX_TOKENS)

    def check(self):
        if self.steps >= MAX_STEPS:
            return "已达步数上限（%d）" % MAX_STEPS
        if time.time() - self.t0 > MAX_SECONDS:
            return "已超时（%ds）" % MAX_SECONDS
        if MAX_TOKENS and self.tokens > MAX_TOKENS:
            return "已达 token 上限（%d）" % MAX_TOKENS
        return None


def maybe_compact(sess, state, budget):
    """压缩 = 内核编排 + 可替换 prompt（.agent/compact.md 覆盖内置默认）。"""
    msgs = sess.project()
    total = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    if total <= COMPACT_CHARS or len(msgs) <= KEEP_TAIL + 1:
        return
    old, tail = msgs[:-KEEP_TAIL], msgs[-KEEP_TAIL:]
    override = cfg_dir() / "compact.md"
    try:
        prompt = override.read_text(encoding="utf-8") if override.is_file() else DEFAULT_COMPACT_PROMPT
    except OSError:
        prompt = DEFAULT_COMPACT_PROMPT
    history = "\n".join("[%s] %s" % (m.get("role"), (m.get("content") or "")[:2000]) for m in old)
    try:
        msg, usage = llm_call([{"role": "user", "content": prompt + history}], state)
        budget.tokens += int(usage.get("total_tokens") or 0)
        summary = (msg.get("content") or "").strip() or "(压缩失败，丢弃早期历史)"
    except Exception as e:
        summary = "(压缩调用失败：%s；丢弃早期历史)" % e
    sess.append("compact", message={"role": "user",
                                    "content": "[早期对话已压缩，摘要如下]\n" + summary})
    sess.append("meta", note="compact 保留最近 %d 条" % KEEP_TAIL)
    for m in tail:  # 尾部消息在日志中位于边界之前，需重放到边界之后投影才可见
        sess.append(m.get("role") if m.get("role") in ("user", "assistant") else "tool",
                    message=m)
    audit("compact", before_chars=total)
    print(c("（上下文已压缩：%d 字符 → 摘要 + 最近 %d 条）" % (total, KEEP_TAIL), "90"))


# ==================== 主循环 ====================

def dispatch(call, sess, state, budget, skills):
    """执行一个工具调用，返回 (工具结果文本, 是否目标达成)。"""
    cid, name, args = call
    if name == "bash":
        result, done = run_bash(str(args.get("command", "")),
                                int(args.get("timeout") or CMD_TIMEOUT), state)
        if done is not None:
            return result, True
    elif name == "read_file":
        result = run_read_file(str(args.get("path", "")), skills)
    else:
        result = "未知工具：%s" % name
    result += budget.line()
    if cid == "text" or state["no_tools"]:
        sess.append("tool", message={"role": "user", "content": "[%s 结果]\n%s" % (name, result)})
    else:
        sess.append("tool", message={"role": "tool", "tool_call_id": cid, "content": result})
    return result, False


def run_turn(sess, state, budget, skills, user_text):
    """一轮：append 用户消息 → 循环（压缩检查 → 调模型 → 执行工具）→ 无工具调用即结束。"""
    sess.append("user", message={"role": "user", "content": user_text})
    while True:
        stop = budget.check()
        if stop:
            print(c("预算护栏触发：" + stop, "33"))
            sess.append("meta", note="budget-stop: " + stop)
            return False
        maybe_compact(sess, state, budget)
        messages = [build_system(skills, state["goal"], state["no_tools"])] + sess.project()
        try:
            msg, usage = llm_call(messages, state)
        except KeyboardInterrupt:
            print(c("\n（已打断模型调用）", "33"))
            return False
        except RuntimeError as e:
            print(c(str(e), "31"))
            sess.append("meta", note="llm-error: " + str(e))
            return False
        budget.steps += 1
        budget.tokens += int(usage.get("total_tokens") or 0)
        sess.append("assistant", message=msg)
        calls = extract_calls(msg)
        if not calls:
            print(msg.get("content") or "(空回复)")
            return False
        if msg.get("content"):
            print(c(msg["content"], "90"))
        for call in calls:
            cmd = (call[2].get("command") or call[2].get("path") or "")
            print(c("→ %s: %s" % (call[1], cmd[:120]), "36"))
            try:
                _, done = dispatch(call, sess, state, budget, skills)
            except KeyboardInterrupt:
                print(c("\n（命令执行已打断）", "33"))
                sess.append("meta", note="tool-interrupted")
                return False
            if done:
                print(c("目标达成。", "32"))
                return True


def goal_loop(sess, state, budget, skills, goal):
    """目标模式：未达成则催促继续，直到 agent-done 或预算耗尽。"""
    done = run_turn(sess, state, budget, skills, goal)
    while not done and not budget.check():
        done = run_turn(sess, state, budget, skills,
                        "若目标已达成，请执行 `agent-done <总结>`；若未达成，请继续下一步行动。")


HELP = """命令：
  /skills     查看已加载与被过滤的技能及原因
  /compact    立即压缩上下文
  /clear      开启新话题（历史保留在日志中，形成分支）
  /exit       退出（也可在空输入时连按两次 Ctrl+C）
其余输入直接作为对话发送。运行中按一次 Ctrl+C 打断当前推理/命令，会话不丢。"""


def repl(sess, state, budget, skills):
    print(c("薄内核 agent | 模型 %s | 会话 %s | /help 查看命令" %
            (AGENT_MODEL or "(未配置)", sess.path or SESSION_MODE_EPHEMERAL), "90"))
    interrupt = 0
    while True:
        try:
            line = input(c("你> ", "1;32")).strip()
            interrupt = 0
        except KeyboardInterrupt:
            interrupt += 1
            print()
            if interrupt >= 2:
                print("再见。")
                return
            print(c("（再按一次 Ctrl+C 退出）", "90"))
            continue
        except EOFError:
            print()
            return
        if not line:
            continue
        if line == "/exit":
            return
        if line == "/help":
            print(HELP)
            continue
        if line == "/skills":
            for s in skills:
                print("  %-16s %s" % (s["name"], s["description"][:60]))
            print(c("  （被过滤的技能见 .agent/audit.jsonl 中 skill/filtered 记录）", "90"))
            continue
        if line == "/compact":
            sess.append("user", message={"role": "user", "content": "（用户手动触发压缩）"})
            maybe_compact_force(sess, state, budget)
            continue
        if line == "/clear":
            sess.append("clear")
            print(c("（已开启新话题，历史仍在日志中）", "90"))
            continue
        run_turn(sess, state, budget, skills, line)


def maybe_compact_force(sess, state, budget):
    global COMPACT_CHARS
    saved, COMPACT_CHARS = COMPACT_CHARS, 0
    try:
        maybe_compact(sess, state, budget)
    finally:
        COMPACT_CHARS = saved


# ==================== 入口 ====================

def main():
    ap = argparse.ArgumentParser(prog="agent.py", description="薄内核单文件智能体")
    ap.add_argument("-a", "--allow-all", action="store_true", help="命令免确认（硬拒绝仍生效）")
    ap.add_argument("-u", "--user-ask", metavar="问题", help="一次性问答（默认不保存会话）")
    ap.add_argument("-s", "--with-session", action="store_true", help="搭配 -u：载入并保存会话")
    ap.add_argument("-g", "--goal", action="store_true", help="目标模式：循环到达成 agent-done")
    ap.add_argument("-l", "--list-session", action="store_true", help="列出本目录会话")
    ap.add_argument("-c", "--clear-session", action="store_true", help="清空本目录会话")
    ap.add_argument("--text", action="store_true", help="强制文本协议（不走 tool calling）")
    args = ap.parse_args()

    if not AGENT_BASE_URL or not AGENT_MODEL:
        sys.exit("请先配置 AGENT_BASE_URL / AGENT_MODEL / AGENT_API_KEY（环境变量，或文件顶部配置区）。")

    sess_dir = cfg_dir() / "sessions"
    if args.list_session:
        if sess_dir.is_dir():
            for p in sorted(sess_dir.glob("*.jsonl"), key=lambda x: -x.stat().st_mtime):
                print("%s  %6d 行  %s" % (p.stem, len(p.read_text(encoding="utf-8").splitlines()),
                                          datetime.fromtimestamp(p.stat().st_mtime).isoformat(" ", "seconds")))
        return
    if args.clear_session:
        sess = Session(persist=True)
        if sess.path and sess.path.exists():
            sess.path.unlink()
            print("已清空 %s" % sess.path)
        return

    skills = load_skills()
    state = {"allow_all": args.allow_all, "goal": args.goal, "no_tools": args.text}
    budget = Budget()
    audit("start", model=AGENT_MODEL, base=AGENT_BASE_URL, skills=[s["name"] for s in skills],
          flags={"a": args.allow_all, "g": args.goal, "text": args.text})

    if args.user_ask is not None:
        sess = Session(persist=args.with_session)
        if args.goal:
            goal_loop(sess, state, budget, skills, args.user_ask)
        else:
            run_turn(sess, state, budget, skills, args.user_ask)
    else:
        repl(Session(persist=True), state, budget, skills)


if __name__ == "__main__":
    main()
