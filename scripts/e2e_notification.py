import json
import sys
import time

try:
    import requests
except ImportError:
    print("requests 未安装，尝试 pip install requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

BASE = "http://127.0.0.1:18923"
TOKEN = "signcheck-demo-token-2024"
HDR = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

SEPARATOR = "=" * 60


def step(name):
    print(f"\n{SEPARATOR}\n  {name}\n{SEPARATOR}")


def api(method, path, **kwargs):
    r = getattr(requests, method)(f"{BASE}{path}", headers=HDR, **kwargs)
    print(f"  {method.upper()} {path} -> {r.status_code}")
    try:
        data = r.json()
    except Exception:
        print(f"  response: {r.text[:200]}")
        return r.status_code, None
    code = data.get("code", -1)
    if code != 0:
        print(f"  code={code} message={data.get('message', '')}")
    return r.status_code, data


# ─── 1. 健康检查 ───
step("1. 健康检查")
status, data = api("get", "/health")
assert status == 200 and data is not None, "健康检查失败"
print(f"  service status: {data['data']['service']['status']}")

# ─── 2. 导入报名数据 ───
step("2. 导入报名数据")
enroll_items = [
    {"name": "张三", "phone": "13800000001", "session": "上午场"},
    {"name": "李四", "phone": "13800000002", "session": "上午场"},
    {"name": "王五", "phone": "13800000003", "session": "上午场"},
    {"name": "赵六", "phone": "13800000004", "session": "上午场"},
    {"name": "钱七", "phone": "13800000005", "session": "上午场"},
]
status, data = api("post", "/api/v1/import/enroll/json", json=enroll_items)
assert status == 200, "导入报名失败"
print(f"  new_count={data['data']['new_count']}")

# ─── 3. 导入签到数据（3人签到，2人缺席） ───
step("3. 导入签到数据")
signin_items = [
    {"name": "张三", "phone": "13800000001", "session": "上午场", "scan_time": "09:01"},
    {"name": "李四", "phone": "13800000002", "session": "上午场", "scan_time": "09:05"},
    {"name": "王五", "phone": "13800000003", "session": "上午场", "scan_time": "09:10"},
]
status, data = api("post", "/api/v1/import/signin/json", json=signin_items)
assert status == 200, "导入签到失败"
print(f"  new_count={data['data']['new_count']}")

# ─── 4. 创建通知规则 ───
step("4. 创建通知规则")
status, data = api("post", "/api/v1/notify/rules", json={
    "session": "上午场",
    "channel": "webhook",
    "target": "http://127.0.0.1:19999/hook",
    "enabled": 1,
    "absent_threshold": 1,
    "extra_config": json.dumps({"headers": {"X-Test-Token": "abc123"}}),
})
assert status == 200, "创建 webhook 规则失败"
webhook_rule_id = data["data"]["id"]
print(f"  webhook rule id={webhook_rule_id}")

status, data = api("post", "/api/v1/notify/rules", json={
    "session": "上午场",
    "channel": "email",
    "target": "admin@example.com",
    "enabled": 1,
    "absent_threshold": 0,
})
assert status == 200, "创建 email 规则失败"
email_rule_id = data["data"]["id"]
print(f"  email rule id={email_rule_id}")

# ─── 5. 列出通知规则 ───
step("5. 列出通知规则")
status, data = api("get", "/api/v1/notify/rules")
assert status == 200, "列出规则失败"
print(f"  count={data['data']['count']}")
for r in data["data"]["rules"]:
    print(f"    id={r['id']} session={r['session']} channel={r['channel']} target={r['target']} threshold={r['absent_threshold']}")

# ─── 6. 执行对账 → 自动触发通知 ───
step("6. 执行对账 → 自动触发通知")
status, data = api("post", "/api/v1/reconcile?sessions=上午场")
assert status == 200, "对账失败"
print(f"  total={data['data']['total']}")
print(f"  status_summary: {json.dumps(data['data']['status_summary'], ensure_ascii=False)}")

notifications = data["data"].get("notifications", {})
print(f"  通知结果:")
for session, items in notifications.items():
    for o in items:
        print(f"    [{session}] channel={o['channel']} target={o['target']} result={o['result']}")
        if o.get("reason"):
            print(f"      reason: {o['reason']}")

# ─── 7. 查询通知日志 ───
step("7. 查询通知日志")
status, data = api("get", "/api/v1/notify/logs")
assert status == 200, "查询日志失败"
print(f"  count={data['data']['count']}")
for l in data["data"]["logs"]:
    print(f"    id={l['id']} channel={l['channel']} target={l['target']} session={l['session']} status={l['status']} retries={l['retries']}")

# ─── 8. 按场次查询通知日志 ───
step("8. 按场次查询通知日志")
status, data = api("get", "/api/v1/notify/logs?session=上午场")
assert status == 200, "按场次查询日志失败"
print(f"  上午场日志 count={data['data']['count']}")

# ─── 9. 更新通知规则（修改阈值） ───
step("9. 更新通知规则")
status, data = api("put", f"/api/v1/notify/rules/{webhook_rule_id}", json={
    "absent_threshold": 10,
})
assert status == 200, "更新规则失败"
print(f"  更新后 threshold={data['data']['rule']['absent_threshold']}")

# ─── 10. 再次对账 → 阈值拦截 ───
step("10. 再次对账 → 阈值拦截（absent=2 < threshold=10）")
status, data = api("post", "/api/v1/reconcile?sessions=上午场")
notifications2 = data["data"].get("notifications", {})
for session, items in notifications2.items():
    for o in items:
        print(f"    [{session}] channel={o['channel']} result={o['result']}", end="")
        if o.get("reason"):
            print(f"  reason={o['reason']}", end="")
        print()

# ─── 11. 删除通知规则 ───
step("11. 删除通知规则")
status, data = api("delete", f"/api/v1/notify/rules/{email_rule_id}")
assert status == 200, "删除规则失败"
print(f"  deleted rule_id={email_rule_id}")

status, data = api("get", "/api/v1/notify/rules")
print(f"  剩余规则 count={data['data']['count']}")

# ─── 完成 ───
print(f"\n{SEPARATOR}")
print("  端到端测试全部通过!")
print(SEPARATOR)
