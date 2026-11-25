"""Округление цены/размера под требования инструмента (GET /v1/instruments)."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def round_price(price: Decimal, prec: dict, rounding=ROUND_HALF_UP) -> Decimal:
    """price_precision: {min_decimal_figures, max_decimal_only_figures, max_significant_figures}.

    * цена >= 1: всегда можно min_decimal_figures знаков после запятой; больше — только если
      всего значащих цифр <= max_significant_figures;
    * цена < 1: не больше max_decimal_only_figures значащих цифр.
    """
    if price <= 0:
        raise ValueError("price must be > 0")
    if price >= 1:
        int_digits = len(str(int(price)))
        decimals = max(prec["min_decimal_figures"], prec["max_significant_figures"] - int_digits)
    else:
        # позиция первой значащей цифры после запятой
        first_sig = -price.adjusted()  # 0.0012 -> adjusted=-3 -> 3
        decimals = first_sig - 1 + prec["max_decimal_only_figures"]
    q = Decimal(1).scaleb(-decimals)
    return price.quantize(q, rounding=rounding).normalize()


def round_size(size: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return size
    return ((size / step).to_integral_value(rounding=ROUND_DOWN) * step).normalize()


def fmt(d: Decimal) -> str:
    """Decimal -> строка без экспоненты (API не принимает scientific notation)."""
    s = format(d.normalize(), "f")
    return s
