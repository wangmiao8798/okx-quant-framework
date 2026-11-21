from pathlib import Path


def test_customer_metrics_route_is_user_scoped():
    text = Path('web/routes.py').read_text(encoding='utf-8')
    assert '@router.get("/user/strategies/{sid}/metrics")' in text
    assert 'uid = int(get_user_id(token))' in text
    assert 'u{uid}_s{sid}' in text
    assert 'build_customer_metrics(strategy_dir' in text
