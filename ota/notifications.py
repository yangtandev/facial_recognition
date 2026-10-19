"""Direct TLS email with durable per-recipient retry; no external service required."""
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
import hashlib
import json
import smtplib
import ssl

from ota.core import timestamp


def validate_smtp(smtp):
    if not isinstance(smtp, dict) or smtp.get("tls") not in ("starttls", "ssl"):
        raise ValueError("SMTP requires starttls or ssl")
    if not smtp.get("host") or "CHANGE_ME" in smtp["host"] or not 1 <= int(smtp.get("port", 0)) <= 65535:
        raise ValueError("Configure SMTP host and port")
    addresses = [smtp.get("from", ""), *smtp.get("to", [])] if isinstance(smtp.get("to"), list) else []
    if len(addresses) < 2 or any(not isinstance(a, str) or any(c in a for c in "\r\n")
            or parseaddr(a)[1] != a or "@" not in a for a in addresses):
        raise ValueError("Configure sender and recipient email addresses")
    if smtp.get("username") and not smtp.get("password"):
        raise ValueError("SMTP password is required for authentication")


def send_mail(config, event, recipient):
    smtp = config["smtp"]
    validate_smtp(smtp)
    message = EmailMessage()
    message["From"], message["To"] = smtp["from"], recipient
    title = {"camera": "攝影機連線異常", "mqtt": "MQTT／建檔同步異常",
             "core": "辨識服務異常", "test": "通知測試"}.get(event.get("alert"), "更新失敗")
    if event.get("kind") == "update":
        title = {"enrollment": "首次接入失敗", "agent_upgrade": "更新器維護失敗"}.get(event.get("operation"), title)
    label = " ".join(f"{event.get('project', 'unknown')} / {event.get('location', 'unknown')}".split())
    message["Subject"] = "OTA " + title + "：" + label
    suffix = hashlib.sha256(recipient.encode()).hexdigest()[:16]
    message["Message-ID"] = f"<{event['event_id']}.{suffix}@facial-ota>"
    message["Date"] = formatdate(localtime=True)
    labels = {"project": "專案", "location": "地點", "device_id": "主機 ID", "role": "角色",
              "from_version": "原版本", "target_version": "目標版本", "running_version": "運行版本",
              "phase": "更新結果", "failed_at": "失敗／異常發生時間",
              "rollback_started_at": "回退開始時間", "rollback_failed_at": "回退失敗當下時間",
              "rollback_completed_at": "回退完成時間", "error": "原因", "rollback": "回退結果",
              "rollback_error": "回退失敗原因", "service_healthy": "服務完整健康"}
    value = dict(event)
    value.setdefault("failed_at", timestamp(event["occurred_at"]))
    if event.get("phase") == "rollback_failed":
        value.setdefault("rollback_failed_at", timestamp(event["occurred_at"]))
    message.set_content("\n".join(f"{label}: {value.get(key, '—')}" for key, label in labels.items())
                        + "\n通知寄送時間: " + timestamp())
    mode = smtp["tls"]
    factory = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    kwargs = {"timeout": 15}
    if mode == "ssl":
        kwargs["context"] = ssl.create_default_context()
    with factory(smtp["host"], int(smtp["port"]), **kwargs) as client:
        if mode == "starttls":
            client.starttls(context=ssl.create_default_context())
        if smtp.get("username"):
            client.login(smtp["username"], smtp["password"])
        refused = client.send_message(message, from_addr=smtp["from"], to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def mail_once(config, store, sender=None):
    sender = sender or send_mail
    store.migrate_notifications()
    for row in store.pending():
        try:
            refused = False
            for recipient in store.recipients(row["id"], config["smtp"]["to"]):
                try:
                    sender(config, json.loads(row["body"]), recipient)
                except smtplib.SMTPRecipientsRefused:
                    refused = True
                    continue
                store.recipient_sent(row["id"], recipient)
            if refused:
                store.defer(row["id"], row["tries"] + 1, "SMTPRecipientsRefused")
            else:
                store.acknowledge(row["id"])
        except Exception as error:
            store.defer(row["id"], row["tries"] + 1, type(error).__name__)
            break  # A shared SMTP outage must not cause 100 consecutive timeouts.
