import pytest

from sidetap_live.cost import Rates, input_seconds, output_seconds


def test_one_minute_of_input():
    # 25 tokens/s x 60 s = 1500 tokens; 1500/1e6 x $3.50
    assert Rates().input_usd(60.0) == pytest.approx(0.00525)


def test_one_minute_of_output():
    assert Rates().output_usd(60.0) == pytest.approx(0.0315)


def test_bytes_convert_at_each_side_s_own_rate():
    """Capture is 16 kHz, playout is 24 kHz. Using one rate for both would
    understate output by a third."""
    assert input_seconds(32_000) == pytest.approx(1.0)
    assert output_seconds(48_000) == pytest.approx(1.0)


def test_rates_are_configuration_not_constants():
    doubled = Rates(input_per_million=7.0)
    assert doubled.input_usd(60.0) == pytest.approx(0.0105)
