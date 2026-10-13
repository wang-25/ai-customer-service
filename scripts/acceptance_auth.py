"""权限认证验收自测（M7-2）。

需要以启用认证的方式启动服务：
    Windows : $env:AUTH_ENABLED="1"; python run.py --port 8030
    Linux   : AUTH_ENABLED=1 python run.py --port 8030

然后：python scripts/acceptance_auth.py --base-url http://127.0.0.1:8030
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8030"
results: list[tuple[str, bool]] = []


def call(method: str, path: str, payload: dict | None = None, token: str = "") -> tuple[int, dict]:
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"{'✅ PASS' if ok else '❌ FAIL'}  {name}" + (f"\n          {detail}" if detail else ""))


def main() -> int:
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE)
    args = parser.parse_args()
    BASE = args.base_url.rstrip("/")

    print("=" * 74)
    print(f"权限认证验收  ->  {BASE}")
    print("=" * 74)

    status, data = call("GET", "/api/auth/status")
    if not data.get("auth_enabled"):
        print("❌ 当前服务未启用认证，请用 AUTH_ENABLED=1 启动后重试")
        return 1
    check("认证已启用并列出四角色", len(data["roles"]) == 4,
          "／".join(f"{r['label']}({r['value']})" for r in data["roles"]))
    check("登录页可获取演示账号", bool(data.get("demo_accounts")),
          f"{len(data.get('demo_accounts', []))} 个演示账号")

    code, _ = call("GET", "/api/agent/sessions")
    check("未登录访问工作台被拒绝（401）", code == 401, f"HTTP {code}")
    code, _ = call("GET", "/api/admin/overview")
    check("未登录访问看板被拒绝（401）", code == 401, f"HTTP {code}")

    code, _ = call("POST", "/api/auth/login", {"username": "admin", "password": "wrong"})
    check("错误密码登录失败（401）", code == 401, f"HTTP {code}")

    tokens: dict[str, str] = {}
    for user, pwd in [("admin", "admin123"), ("supervisor", "123456"),
                      ("agent1", "123456"), ("editor", "123456")]:
        code, res = call("POST", "/api/auth/login", {"username": user, "password": pwd})
        tokens[user] = res.get("token", "")
        check(f"账号 {user} 登录成功并返回角色", code == 200 and bool(tokens[user]),
              f"角色={res.get('account', {}).get('role_label', '')}")

    code, _ = call("GET", "/api/auth/me", token=tokens["agent1"])
    check("携带 Token 可获取当前账号", code == 200, "GET /api/auth/me")

    # 客服：可用工作台，不可用看板与知识库
    code, _ = call("GET", "/api/agent/sessions", token=tokens["agent1"])
    check("客服角色可访问工作台", code == 200, f"HTTP {code}")
    code, res = call("GET", "/api/admin/overview", token=tokens["agent1"])
    check("客服角色被拒绝访问数据看板（403）", code == 403,
          f"HTTP {code}：{res.get('detail', '')}")
    code, res = call("GET", "/api/knowledge", token=tokens["agent1"])
    check("客服角色被拒绝访问知识库管理（403）", code == 403,
          f"HTTP {code}：{res.get('detail', '')}")

    # 知识运营：可用知识库与文档导入，不可用看板
    code, _ = call("GET", "/api/knowledge", token=tokens["editor"])
    check("知识运营可访问知识库", code == 200, f"HTTP {code}")
    code, _ = call("GET", "/api/admin/fallbacks", token=tokens["editor"])
    check("知识运营可访问 Badcase 回流", code == 200, f"HTTP {code}")
    code, res = call("GET", "/api/admin/overview", token=tokens["editor"])
    check("知识运营被拒绝访问数据看板（403）", code == 403, f"HTTP {code}")
    code, res = call("GET", "/api/admin/accounts", token=tokens["editor"])
    check("知识运营被拒绝管理账号（403）", code == 403, f"HTTP {code}")

    # 主管：可用看板，不可管理账号
    code, _ = call("GET", "/api/admin/overview", token=tokens["supervisor"])
    check("客服主管可访问数据看板", code == 200, f"HTTP {code}")
    code, res = call("GET", "/api/admin/accounts", token=tokens["supervisor"])
    check("客服主管被拒绝管理账号（403）", code == 403,
          f"HTTP {code}：{res.get('detail', '')}")

    # 管理员：全通
    for path in ["/api/admin/overview", "/api/admin/accounts", "/api/knowledge", "/api/agent/sessions"]:
        code, _ = call("GET", path, token=tokens["admin"])
        check(f"管理员可访问 {path}", code == 200, f"HTTP {code}")

    code, res = call("DELETE", "/api/admin/accounts/1", token=tokens["admin"])
    check("不能删除当前登录账号", code == 400, f"HTTP {code}：{res.get('detail', '')}")

    print()
    print("=" * 74)
    passed = sum(1 for _, ok in results if ok)
    print(f"结果：{passed}/{len(results)} 项通过")
    failed = [n for n, ok in results if not ok]
    if failed:
        print("未通过：")
        for n in failed:
            print(f"  - {n}")
    print("=" * 74)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
