import json
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

from .models import NotificationLog, NotificationRule, ReconcileResult

logger = logging.getLogger("signcheck.notifier")

WEBHOOK_MAX_RETRIES = 2


def _load_smtp_config() -> Dict[str, Any]:
    env_keys = {
        "host": "SIGNCHECK_SMTP_HOST",
        "port": "SIGNCHECK_SMTP_PORT",
        "user": "SIGNCHECK_SMTP_USER",
        "password": "SIGNCHECK_SMTP_PASSWORD",
        "from_addr": "SIGNCHECK_SMTP_FROM",
        "use_tls": "SIGNCHECK_SMTP_USE_TLS",
    }
    config: Dict[str, Any] = {}
    for key, env_key in env_keys.items():
        val = os.environ.get(env_key)
        if val is not None:
            if key == "port":
                config[key] = int(val)
            elif key == "use_tls":
                config[key] = val.lower() in ("1", "true", "yes")
            else:
                config[key] = val

    config_dir = os.environ.get("SIGNCHECK_DB_DIR")
    if config_dir:
        cfg_path = os.path.join(config_dir, ".signcheck", "notify.json")
    else:
        cfg_path = os.path.join(os.getcwd(), ".signcheck", "notify.json")

    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            smtp_cfg = file_cfg.get("smtp", {})
            for key in ("host", "port", "user", "password", "from_addr", "use_tls"):
                if key not in config and key in smtp_cfg:
                    if key == "port":
                        config[key] = int(smtp_cfg[key])
                    elif key == "use_tls":
                        config[key] = bool(smtp_cfg[key])
                    else:
                        config[key] = smtp_cfg[key]
        except (json.JSONDecodeError, IOError, ValueError):
            pass

    config.setdefault("host", "localhost")
    config.setdefault("port", 25)
    config.setdefault("use_tls", False)
    config.setdefault("from_addr", config.get("user", "signcheck@localhost"))
    return config


SMTP_CONFIG = _load_smtp_config()


def build_session_summary(session: str, results: List[ReconcileResult]) -> Dict[str, Any]:
    session_results = [r for r in results if r.session == session]
    total = len(session_results)
    counts: Dict[str, int] = {"normal": 0, "absent": 0, "non_enrolled": 0, "duplicate": 0}
    for r in session_results:
        if r.status in counts:
            counts[r.status] += 1
    absent_names = [r.name for r in session_results if r.status == "absent"]
    return {
        "session": session,
        "total": total,
        "counts": counts,
        "absent_names": absent_names,
    }


def format_summary_text(summary: Dict[str, Any]) -> str:
    lines = [
        f"[签到对账结果] 场次: {summary['session']}",
        f"总人数: {summary['total']}",
        f"正常签到: {summary['counts']['normal']}",
        f"缺席: {summary['counts']['absent']}",
        f"未报名: {summary['counts']['non_enrolled']}",
        f"重复签到: {summary['counts']['duplicate']}",
    ]
    if summary["absent_names"]:
        lines.append(f"缺席人员: {', '.join(summary['absent_names'])}")
    return "\n".join(lines)


def should_notify(rule: NotificationRule, summary: Dict[str, Any]) -> bool:
    if not rule.enabled:
        return False
    if rule.absent_threshold > 0:
        if summary["counts"]["absent"] < rule.absent_threshold:
            return False
    return True


def send_email(to_addr: str, subject: str, body: str) -> Tuple[bool, str]:
    cfg = SMTP_CONFIG
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg["from_addr"]
        msg["To"] = to_addr

        if cfg.get("use_tls"):
            server = smtplib.SMTP(cfg["host"], cfg["port"])
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"])

        if cfg.get("user") and cfg.get("password"):
            server.login(cfg["user"], cfg["password"])

        server.sendmail(cfg["from_addr"], [to_addr], msg.as_string())
        server.quit()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def send_webhook(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Tuple[bool, str, int]:
    import urllib.request
    import urllib.error

    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")

    retries_done = 0
    last_error = ""
    for attempt in range(1 + WEBHOOK_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
                return True, "ok", retries_done
        except Exception as exc:
            retries_done += 1
            last_error = str(exc)
            if attempt < WEBHOOK_MAX_RETRIES:
                logger.warning(
                    "webhook retry %d/%d for %s: %s",
                    retries_done, WEBHOOK_MAX_RETRIES, url, last_error,
                )
                time.sleep(1)

    return False, last_error, retries_done


def notify_session(
    session: str,
    results: List[ReconcileResult],
    rules: List[NotificationRule],
    log_callback: Any = None,
) -> List[Dict[str, Any]]:
    summary = build_session_summary(session, results)
    text = format_summary_text(summary)
    subject = f"[对账结果] 场次 {session} - 缺席 {summary['counts']['absent']} 人"
    outcomes: List[Dict[str, Any]] = []

    for rule in rules:
        if not should_notify(rule, summary):
            outcomes.append({
                "rule_id": rule.id,
                "channel": rule.channel,
                "target": rule.target,
                "session": session,
                "result": "skipped",
                "reason": f"absent_count={summary['counts']['absent']} < threshold={rule.absent_threshold}",
            })
            continue

        if rule.channel == "email":
            ok, msg = send_email(rule.target, subject, text)
            outcome = {
                "rule_id": rule.id,
                "channel": rule.channel,
                "target": rule.target,
                "session": session,
                "result": "success" if ok else "failed",
                "message": msg,
                "retries": 0,
            }
            if log_callback:
                log_callback(NotificationLog(
                    channel=rule.channel,
                    target=rule.target,
                    session=session,
                    status="success" if ok else "failed",
                    message=msg,
                    retries=0,
                ))
            outcomes.append(outcome)

        elif rule.channel == "webhook":
            extra = {}
            if rule.extra_config:
                try:
                    extra = json.loads(rule.extra_config)
                except (json.JSONDecodeError, TypeError):
                    extra = {}

            custom_headers = extra.get("headers")
            payload = {
                "session": session,
                "summary": summary,
                "text": text,
            }
            ok, msg, retries = send_webhook(rule.target, payload, headers=custom_headers)
            outcome = {
                "rule_id": rule.id,
                "channel": rule.channel,
                "target": rule.target,
                "session": session,
                "result": "success" if ok else "failed",
                "message": msg,
                "retries": retries,
            }
            if log_callback:
                log_callback(NotificationLog(
                    channel=rule.channel,
                    target=rule.target,
                    session=session,
                    status="success" if ok else "failed",
                    message=msg,
                    retries=retries,
                ))
            outcomes.append(outcome)
        else:
            outcomes.append({
                "rule_id": rule.id,
                "channel": rule.channel,
                "target": rule.target,
                "session": session,
                "result": "skipped",
                "reason": f"unknown channel: {rule.channel}",
            })

    return outcomes


def notify_after_reconcile(
    sessions: List[str],
    results: List[ReconcileResult],
    storage: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    all_outcomes: Dict[str, List[Dict[str, Any]]] = {}

    def log_cb(log: NotificationLog):
        try:
            storage.add_notification_log(log)
        except Exception as exc:
            logger.error("failed to write notification log: %s", exc)

    for session in sessions:
        rules = storage.get_notification_rules_by_session(session)
        if not rules:
            continue
        outcomes = notify_session(session, results, rules, log_callback=log_cb)
        all_outcomes[session] = outcomes

    return all_outcomes
