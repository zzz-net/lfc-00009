import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from signcheck.models import NotificationLog, NotificationRule, ReconcileResult
from signcheck.notifier import (
    build_session_summary,
    format_summary_text,
    notify_after_reconcile,
    notify_session,
    send_email,
    send_webhook,
    should_notify,
)
from signcheck.storage import Storage

SEPARATOR = "=" * 60


def make_results(session, normal=0, absent=0, non_enrolled=0, duplicate=0):
    results = []
    for i in range(normal):
        results.append(ReconcileResult(name=f"正常{i}", phone=f"1{i}", session=session, status="normal"))
    for i in range(absent):
        results.append(ReconcileResult(name=f"缺席{i}", phone=f"2{i}", session=session, status="absent"))
    for i in range(non_enrolled):
        results.append(ReconcileResult(name=f"未报名{i}", phone=f"3{i}", session=session, status="non_enrolled"))
    for i in range(duplicate):
        results.append(ReconcileResult(name=f"重复{i}", phone=f"4{i}", session=session, status="duplicate"))
    return results


def scenario_1_normal_push():
    print(SEPARATOR)
    print("场景1: 正常推送 - 缺席人数超过阈值，webhook 和 email 都触发")
    print(SEPARATOR)

    db_path = os.path.join(tempfile.mkdtemp(), "test1.db")
    storage = Storage(db_path=db_path)

    storage.add_notification_rule(NotificationRule(
        session="场次A", channel="webhook", target="http://127.0.0.1:19991/hook",
        enabled=1, absent_threshold=1,
    ))
    storage.add_notification_rule(NotificationRule(
        session="场次A", channel="email", target="admin@example.com",
        enabled=1, absent_threshold=0,
    ))

    results = make_results("场次A", normal=5, absent=3, non_enrolled=1, duplicate=0)
    summary = build_session_summary("场次A", results)
    print(f"  场次摘要: {json.dumps(summary, ensure_ascii=False)}")

    text = format_summary_text(summary)
    print(f"  通知正文:\n{text}")

    with patch("signcheck.notifier.send_webhook") as mock_wh, \
         patch("signcheck.notifier.send_email") as mock_mail:
        mock_wh.return_value = (True, "ok", 0)
        mock_mail.return_value = (True, "ok")

        outcomes = notify_after_reconcile(["场次A"], results, storage)

    print(f"  推送结果:")
    for session, items in outcomes.items():
        for o in items:
            print(f"    [{session}] channel={o['channel']} target={o['target']} result={o['result']}")

    logs = storage.get_notification_logs(limit=10)
    print(f"  通知日志 ({len(logs)} 条):")
    for l in logs:
        print(f"    id={l.id} channel={l.channel} target={l.target} status={l.status} retries={l.retries}")

    storage.close()
    print()


def scenario_2_threshold_block():
    print(SEPARATOR)
    print("场景2: 阈值拦截 - 缺席人数未达阈值，通知被拦截")
    print(SEPARATOR)

    db_path = os.path.join(tempfile.mkdtemp(), "test2.db")
    storage = Storage(db_path=db_path)

    storage.add_notification_rule(NotificationRule(
        session="场次B", channel="webhook", target="http://example.com/hook",
        enabled=1, absent_threshold=5,
    ))

    results = make_results("场次B", normal=8, absent=2, non_enrolled=0, duplicate=0)
    summary = build_session_summary("场次B", results)
    print(f"  缺席人数: {summary['counts']['absent']}, 阈值: 5")

    with patch("signcheck.notifier.send_webhook") as mock_wh:
        mock_wh.return_value = (True, "ok", 0)

        outcomes = notify_after_reconcile(["场次B"], results, storage)

    for session, items in outcomes.items():
        for o in items:
            print(f"    [{session}] channel={o['channel']} result={o['result']} reason={o.get('reason', '')}")

    logs = storage.get_notification_logs(limit=10)
    print(f"  通知日志 ({len(logs)} 条): 阈值拦截不记录日志")

    storage.close()
    print()


def scenario_3_webhook_retry():
    print(SEPARATOR)
    print("场景3: Webhook 重试 - 前2次失败后第3次成功，测试重试机制")
    print(SEPARATOR)

    db_path = os.path.join(tempfile.mkdtemp(), "test3.db")
    storage = Storage(db_path=db_path)

    storage.add_notification_rule(NotificationRule(
        session="场次C", channel="webhook", target="http://127.0.0.1:19992/hook",
        enabled=1, absent_threshold=0,
        extra_config=json.dumps({"headers": {"X-Custom-Token": "abc123"}}),
    ))

    results = make_results("场次C", normal=5, absent=2, non_enrolled=0, duplicate=0)

    urlopen_call_count = 0

    def fake_urlopen(req, timeout=10):
        nonlocal urlopen_call_count
        urlopen_call_count += 1
        if urlopen_call_count <= 2:
            print(f"    urllib.urlopen 第 {urlopen_call_count} 次调用 -> 抛出异常 (模拟网络错误)")
            raise Exception("Connection refused")
        print(f"    urllib.urlopen 第 {urlopen_call_count} 次调用 -> 成功 (模拟)")
        class FakeResp:
            def read(self):
                return b'{"ok":true}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        outcomes = notify_after_reconcile(["场次C"], results, storage)

    for session, items in outcomes.items():
        for o in items:
            print(f"    [{session}] channel={o['channel']} result={o['result']} retries={o['retries']}")

    logs = storage.get_notification_logs(limit=10)
    print(f"  通知日志 ({len(logs)} 条):")
    for l in logs:
        print(f"    id={l.id} channel={l.channel} status={l.status} retries={l.retries} message={l.message}")

    print()
    print("  --- 子场景: 全部重试失败 ---")
    urlopen_call_count2 = 0

    def always_fail_urlopen(req, timeout=10):
        nonlocal urlopen_call_count2
        urlopen_call_count2 += 1
        print(f"    urllib.urlopen 第 {urlopen_call_count2} 次调用 -> 失败")
        raise Exception("Connection timeout")

    db_path2 = os.path.join(tempfile.mkdtemp(), "test3b.db")
    storage2 = Storage(db_path=db_path2)
    storage2.add_notification_rule(NotificationRule(
        session="场次C", channel="webhook", target="http://127.0.0.1:19993/hook",
        enabled=1, absent_threshold=0,
    ))

    with patch("urllib.request.urlopen", side_effect=always_fail_urlopen):
        outcomes2 = notify_after_reconcile(["场次C"], results, storage2)

    for session, items in outcomes2.items():
        for o in items:
            print(f"    [{session}] channel={o['channel']} result={o['result']} retries={o['retries']} message={o.get('message','')}")

    logs2 = storage2.get_notification_logs(limit=10)
    print(f"  通知日志 ({len(logs2)} 条):")
    for l in logs2:
        print(f"    id={l.id} channel={l.channel} status={l.status} retries={l.retries} message={l.message}")

    storage.close()
    storage2.close()
    print()


def scenario_4_email_send():
    print(SEPARATOR)
    print("场景4: 邮件发送 - 通过 SMTP 发送通知邮件")
    print(SEPARATOR)

    db_path = os.path.join(tempfile.mkdtemp(), "test4.db")
    storage = Storage(db_path=db_path)

    storage.add_notification_rule(NotificationRule(
        session="场次D", channel="email", target="admin@example.com",
        enabled=1, absent_threshold=1,
    ))

    results = make_results("场次D", normal=10, absent=3, non_enrolled=1, duplicate=0)
    summary = build_session_summary("场次D", results)
    print(f"  场次摘要: normal={summary['counts']['normal']} absent={summary['counts']['absent']}")

    with patch("signcheck.notifier.send_email") as mock_mail:
        mock_mail.return_value = (True, "ok")

        outcomes = notify_after_reconcile(["场次D"], results, storage)

    for session, items in outcomes.items():
        for o in items:
            print(f"    [{session}] channel={o['channel']} target={o['target']} result={o['result']}")

    logs = storage.get_notification_logs(limit=10)
    print(f"  通知日志 ({len(logs)} 条):")
    for l in logs:
        print(f"    id={l.id} channel={l.channel} target={l.target} status={l.status}")

    with patch("signcheck.notifier.send_email") as mock_mail_fail:
        mock_mail_fail.return_value = (False, "SMTP Authentication failed")

        outcomes2 = notify_after_reconcile(["场次D"], results, storage)

    for session, items in outcomes2.items():
        for o in items:
            print(f"    [{session}] channel={o['channel']} result={o['result']} message={o.get('message', '')}")

    storage.close()
    print()


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║         SignCheck 通知模块模拟测试                        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    scenario_1_normal_push()
    scenario_2_threshold_block()
    scenario_3_webhook_retry()
    scenario_4_email_send()

    print(SEPARATOR)
    print("所有场景测试完成!")
    print(SEPARATOR)
