import logging

from utils.notifier import Notifier


class ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class SMTPStub:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def login(self, email, password):
        self.calls.append(("login", email, password))

    def send_message(self, _message):
        self.calls.append(("send",))


def test_dict_config_error_email_and_deduplication(monkeypatch):
    monkeypatch.setattr("utils.notifier.threading.Thread", ImmediateThread)
    notifier = Notifier({
        "notify": {
            "email": {
                "enabled": True,
                "min_interval": 61,
            }
        }
    })
    sent = []
    notifier.send_email = lambda subject, body: sent.append((subject, body)) or True

    assert notifier.alert("风险停机", "DRAM stopped", key="risk:dram") is True
    assert notifier.alert("风险停机", "DRAM stopped", key="risk:dram") is False

    logger = logging.getLogger("notifier-test")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    notifier.attach_error_logger(logger)
    logger.error("order reconciliation failed")

    assert len(sent) == 2
    assert sent[0][0] == "[量化告警] 风险停机"
    assert "order reconciliation failed" in sent[1][1]


def test_email_password_can_be_loaded_from_a_process_environment(monkeypatch):
    calls = []
    monkeypatch.setenv("TEST_NOTIFY_PASSWORD", "test-env-password")
    monkeypatch.setattr(
        "utils.notifier.smtplib.SMTP_SSL",
        lambda *_args, **_kwargs: SMTPStub(calls),
    )
    notifier = Notifier({
        "notify": {
            "email": {
                "enabled": True,
                "email": "sender@example.test",
                "to": "recipient@example.test",
                "password_env": "TEST_NOTIFY_PASSWORD",
            }
        }
    })

    assert notifier.send_email("subject", "body") is True
    assert calls == [
        ("login", "sender@example.test", "test-env-password"),
        ("send",),
    ]
