#!/usr/bin/env python3
"""run_smoke.py —— 薄内核端到端冒烟测试（mock LLM，无需真实模型）。
用法: python3 tests/run_smoke.py
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
FAILURES = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILURES.append(msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def new_project():
    tmp = Path(tempfile.mkdtemp(prefix="agent-smoke-"))
    shutil.copytree(str(ROOT / "skills"), str(tmp / "skills"))
    shutil.copy(str(ROOT / "demo" / "demo.csv"), str(tmp / "demo.csv"))
    return tmp


def bash_call(cmd, cid="c1"):
    return {"reply": {"tool_calls": [{"id": cid, "type": "function", "function": {
        "name": "bash", "arguments": json.dumps({"command": cmd})}}]}}


def say(text, match=None):
    step = {"reply": {"role": "assistant", "content": text}}
    if match:
        step["match"] = match
    return step


def invoke(tmp, steps, args, extra_env=None, no_tools=False):
    """启动一次 mock 服务并跑一次 agent，返回 (stdout, reqlog行, audit行)。"""
    port = free_port()
    scenario = tmp / ("scenario-%d.json" % port)
    reqlog = tmp / ("reqlog-%d.jsonl" % port)
    json.dump(steps, open(str(scenario), "w", encoding="utf-8"), ensure_ascii=False)
    env = dict(os.environ)
    env.update({"AGENT_BASE_URL": "http://127.0.0.1:%d/v1" % port,
                "AGENT_MODEL": "mock-model", "AGENT_API_KEY": "x",
                "MOCK_PORT": str(port), "MOCK_SCENARIO": str(scenario),
                "MOCK_REQLOG": str(reqlog)})
    if no_tools:
        env["MOCK_NO_TOOLS"] = "1"
    if extra_env:
        env.update(extra_env)
    srv = subprocess.Popen([PY, str(ROOT / "tests" / "mock_llm_server.py")], env=env)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        out = subprocess.run([PY, str(ROOT / "agent.py")] + args, cwd=str(tmp), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, universal_newlines=True,
                             timeout=60).stdout
    finally:
        srv.send_signal(signal.SIGTERM)
        srv.wait(timeout=5)
    reqs = [json.loads(l) for l in reqlog.read_text(encoding="utf-8").splitlines() if l.strip()]
    audit_file = tmp / ".agent" / "audit.jsonl"
    audits = [json.loads(l) for l in audit_file.read_text(encoding="utf-8").splitlines()] \
        if audit_file.exists() else []
    return out, reqs, audits


print("S1 tools 模式 + 技能脚本执行（-a 一次性）")
tmp = new_project()
out, reqs, audits = invoke(tmp, [
    bash_call("python3 %s/skills/csv-stats/scripts/stats.py %s/demo.csv" % (tmp, tmp)),
    say("统计完成：共 6 行，价格列有 1 个缺失。"),
], ["-a", "-u", "统计一下 demo.csv"])
check("统计完成" in out, "模型最终答复打印")
check(any(a.get("action") == "exec" and "stats.py" in a.get("cmd", "") for a in audits),
      "审计日志记录脚本执行")
check(any(a.get("action") == "skill" and a.get("name") == "csv-stats"
          and a.get("decision") == "loaded" for a in audits), "csv-stats 技能加载")
check(not (tmp / ".agent" / "sessions").exists(), "一次性模式不落会话文件")

print("S2 文本协议降级（端点拒绝 tools）")
tmp = new_project()
out, reqs, audits = invoke(tmp, [
    say("先读文件：\n```bash\nhead -2 %s/demo.csv\n```" % tmp),
    say("读取完成，前两行是表头和笔记本。"),
], ["-u", "读一下文件开头"], no_tools=True)
check("读取完成" in out, "文本模式完成一轮")
check("tools" in reqs[0] and all("tools" not in r for r in reqs[1:]),
      "首次带 tools 被拒后自动降级，后续请求不再携带")
check(any(a.get("event") == "fallback-to-text-mode" for a in audits), "降级记入审计")
check(any(a.get("action") == "gate" and a.get("decision") == "allow" for a in audits),
      "head 纯读命令自动放行")

print("S3 写命令在非交互模式被拒绝")
tmp = new_project()
(tmp / "secret.txt").write_text("机密", encoding="utf-8")
out, reqs, audits = invoke(tmp, [
    bash_call("rm -rf %s/secret.txt" % tmp),
    say("好的，已放弃删除。"),
], ["-u", "删掉 secret.txt"])
check((tmp / "secret.txt").exists(), "文件未被删除")
check(any(a.get("decision") == "deny-noninteractive" for a in audits), "拒绝决策留痕")

print("S4 硬拒绝不受 -a 豁免")
tmp = new_project()
out, reqs, audits = invoke(tmp, [
    bash_call("mkfs.ext4 /dev/sda"),
    say("该命令已被安全策略硬拒绝。"),
], ["-a", "-u", "格式化磁盘"])
check("硬拒绝" in out, "模型收到硬拒绝结果")
check(any(a.get("decision") == "deny" for a in audits), "硬拒绝留痕")

print("S5 目录级会话续接（-s 两次调用）")
tmp = new_project()
out1, _, _ = invoke(tmp, [say("已记住：5432。")], ["-s", "-u", "记住：数据库端口是 5432"])
out2, reqs2, _ = invoke(tmp, [
    say("5432。", match="数据库端口是 5432"),
], ["-s", "-u", "端口是多少"])
check("5432。" in out2, "第二轮回答")
check(any("数据库端口是 5432" in json.dumps(r, ensure_ascii=False) for r in reqs2),
      "第二轮请求携带首轮历史（会话续接）")
check(not any("MATCH_FAIL" in r for r in reqs2), "match 断言通过")

print("S6 上下文压缩（超低阈值强制触发）")
tmp = new_project()
steps = [bash_call("echo hello-%d" % i, cid="c%d" % i) for i in range(4)] + [say("第一轮完成。")]
invoke(tmp, steps, ["-s", "-u", "第一轮"])
out, reqs, audits = invoke(tmp, [say("第二轮完成。")], ["-s", "-u", "第二轮"],
                           extra_env={"AGENT_COMPACT_CHARS": "300"})
check(any("你是压缩器" in json.dumps(r, ensure_ascii=False) for r in reqs), "压缩调用发生")
real = [r for r in reqs if "你是压缩器" not in json.dumps(r, ensure_ascii=False)]
check(any("早期对话已压缩" in json.dumps(r["messages"], ensure_ascii=False) for r in real),
      "后续请求基于压缩摘要投影")
check(any(a.get("action") == "compact" for a in audits), "压缩留痕")

print("S7 目标模式 agent-done 哨兵")
tmp = new_project()
out, reqs, audits = invoke(tmp, [
    bash_call("agent-done 测试目标已完成"),
], ["-a", "-g", "-u", "完成目标测试"])
check("目标达成" in out, "agent-done 被内核拦截并终止目标循环")
check(not any(a.get("action") == "exec" and "agent-done" in a.get("cmd", "") for a in audits),
      "agent-done 未进入真实 shell")

print()
if FAILURES:
    print("失败 %d 项：" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("全部通过。")
