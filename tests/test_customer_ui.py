from pathlib import Path


def test_customer_shell_is_available_without_api_key():
    html = (Path(__file__).parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'onclick="logout()">退出登录</button>' in html
    assert ".customer-main{display:none;flex:1;width:100%;min-width:0}" in html
    assert "customerInit();\n    customerLoad();" in html
    assert "['home','income','quant','mine'].forEach(customerRender);" in html
    assert "添加前也可以正常浏览客户页面" in html


def test_web_service_uses_nginx_local_upstream_port():
    unit = (Path(__file__).parents[1] / "systemd" / "okx-quant-web.service").read_text(
        encoding="utf-8"
    )

    assert "--host 127.0.0.1 --port 8799" in unit
