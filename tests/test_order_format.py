from api.okx_client import OKXClient


def test_order_size_normalizes_float_artifacts():
    assert OKXClient._format_order_size(0.7000000000000001) == "0.7"
    assert OKXClient._format_order_size(1.4000000000000001) == "1.4"
    assert OKXClient._format_order_size("0.5") == "0.5"
