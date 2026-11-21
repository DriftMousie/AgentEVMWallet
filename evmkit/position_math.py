"""Integer Q64.96 price and principal calculations for concentrated liquidity."""

import re
from fractions import Fraction
from math import isqrt

from .common import formatted, require, uint

Q96 = 1 << 96
MIN_TICK, MAX_TICK = -887272, 887272
MIN_SQRT, MAX_SQRT = 4295128739, 1461446703485210103287273052203988822378723970342
MAX_LIQUIDITY = (1 << 127) - 1
# Q128.128 factors for sqrt(1.0001 ** -(2**i)). See Uniswap v3-core TickMath.
FACTORS = (
    0xfffcb933bd6fad37aa2d162d1a594001, 0xfff97272373d413259a46990580e213a,
    0xfff2e50f5f656932ef12357cf3c7fdcc, 0xffe5caca7e10e4e61c3624eaa0941cd0,
    0xffcb9843d60f6159c9db58835c926644, 0xff973b41fa98c081472e6896dfb254c0,
    0xff2ea16466c96a3843ec78b326b52861, 0xfe5dee046a99a2a811c461f1969c3053,
    0xfcbe86c7900a88aedcffc83b479aa3a4, 0xf987a7253ac413176f2b074cf7815e54,
    0xf3392b0822b70005940c7a398e4b70f3, 0xe7159475a2c29b7443b29c7fa6e889d9,
    0xd097f3bdfd2022b8845ad8f792aa5825, 0xa9f746462d870fdf8a65dc1f90e061e5,
    0x70d869a156d2a1b890bb3df62baf32f7, 0x31be135f97d08fd981231505542fcfa6,
    0x9aa508b5b7a84e1c677de54f3e99bc9, 0x5d6af8dedb81196699c329225ee604,
    0x2216e584f5fa1ea926041bedfe98, 0x48a170391f7dc42444e8fa2,
)
FORMULAS = ("tick2sqrt", "sqrt2tick", "tick2price", "price2tick", "amounts2liquidity", "liquidity2amounts")
MATH_FIELDS = {
    "tick2sqrt": {"tick"},
    "sqrt2tick": {"sqrt_price_x96"},
    "tick2price": {"tick", "decimals0", "decimals1"},
    "price2tick": {"price", "decimals0", "decimals1"},
    "amounts2liquidity": {"tick_current", "sqrt_price_x96", "tick_lower", "tick_upper", "amount0", "amount1", "decimals0", "decimals1", "round"},
    "liquidity2amounts": {"tick_current", "sqrt_price_x96", "tick_lower", "tick_upper", "liquidity", "decimals0", "decimals1", "round"},
}


def quotient(numerator, denominator, mode):
    value, remainder = divmod(numerator, denominator)
    return value + int(mode == "up" and bool(remainder))


def tick_number(value):
    require(isinstance(value, (str, int)) and not isinstance(value, bool)
            and re.fullmatch(r"-?[0-9]{1,7}", str(value)), "Tick must be a signed integer")
    tick = int(value)
    require(MIN_TICK <= tick <= MAX_TICK, "Tick outside protocol range")
    return tick


def tick_to_sqrt(tick):
    tick = tick_number(tick)
    ratio = 1 << 128
    for bit, factor in enumerate(FACTORS):
        if abs(tick) & (1 << bit):
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = ((1 << 256) - 1) // ratio
    return quotient(ratio, 1 << 32, "up")


def sqrt_to_tick(price):
    require(MIN_SQRT <= price < MAX_SQRT, "sqrtPriceX96 outside active pool range")
    low, high = MIN_TICK, MAX_TICK
    while low + 1 < high:
        middle = (low + high) // 2
        if tick_to_sqrt(middle) <= price:
            low = middle
        else:
            high = middle
    return low


def decimal_scale(params):
    scales = [uint(params.get(name, 0), name) for name in ("decimals0", "decimals1")]
    require(max(scales) <= 255, "Token decimals out of range")
    return Fraction(10 ** scales[0], 10 ** scales[1])


def decimal_string(fraction, places=36):
    number = fraction.numerator * (10 ** places) // fraction.denominator
    whole, part = divmod(number, 10 ** places)
    return (f"{whole}.{part:0{places}d}").rstrip("0").rstrip(".")


def position_factors(current, lower, upper):
    """Token requirements per unit of liquidity as exact fractions."""
    active = min(upper, max(lower, current))
    return (
        Fraction(Q96 * (upper - active), upper * active),
        Fraction(active - lower, Q96),
    )


def calculate(params):
    formula = params.get("formula")
    require(formula in FORMULAS, "Choose a math formula")
    require(not set(params) - MATH_FIELDS[formula] - {"formula", "chain"}, "Unexpected parameter for this math formula")
    out = {"formula":formula}
    if formula in ("tick2sqrt", "tick2price"):
        tick = tick_number(params.get("tick"))
        sqrt = tick_to_sqrt(tick)
        out.update(tick=tick, sqrtPriceX96=str(sqrt))
        if formula == "tick2price":
            price = Fraction(sqrt * sqrt, Q96 * Q96) * decimal_scale(params)
            out.update(price=decimal_string(price), price_numerator=str(price.numerator),
                       price_denominator=str(price.denominator), price_unit="token1_per_token0",
                       price_round="down", price_decimal_places=36)
        return out
    if formula in ("sqrt2tick", "price2tick"):
        if formula == "price2tick":
            price = params.get("price")
            require(isinstance(price, str) and len(price) <= 256 and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", price), "Price must be a positive decimal string")
            fraction = Fraction(price) / decimal_scale(params)
            scaled = fraction * Q96 * Q96
            sqrt = isqrt(scaled.numerator // scaled.denominator)
        else:
            sqrt = uint(params.get("sqrt_price_x96"), "sqrt_price_x96")
        return {**out, "sqrtPriceX96":str(sqrt), "tick":sqrt_to_tick(sqrt)}

    lower_tick = tick_number(params.get("tick_lower"))
    upper_tick = tick_number(params.get("tick_upper"))
    require(lower_tick < upper_tick, "tick_lower must be below tick_upper")
    require((params.get("tick_current") is None) != (params.get("sqrt_price_x96") is None),
            "Supply exactly one of tick_current and sqrt_price_x96")
    exact_price = params.get("sqrt_price_x96") is not None
    if exact_price:
        current = uint(params["sqrt_price_x96"], "sqrt_price_x96")
        current_tick = sqrt_to_tick(current)
    else:
        current_tick = tick_number(params["tick_current"])
        current = tick_to_sqrt(current_tick)
    lower, upper = tick_to_sqrt(lower_tick), tick_to_sqrt(upper_tick)
    factors = position_factors(current, lower, upper)
    default_round = "down" if formula == "amounts2liquidity" else "up"
    mode = params.get("round", default_round)
    require(mode in ("up", "down"), "round must be up or down")
    budgets = None
    if formula == "amounts2liquidity":
        budgets = [uint(params.get(f"amount{i}"), f"amount{i}") for i in range(2)]
        limits = (quotient(amount * factor.denominator, factor.numerator, mode)
                  for amount, factor in zip(budgets, factors) if factor)
        liquidity = min(limits)
    else:
        liquidity = uint(params.get("liquidity"), "liquidity")
    require(liquidity <= MAX_LIQUIDITY, "Liquidity exceeds signed int128 mint delta limit")
    amount_round = "up" if budgets is not None else mode
    amounts = [quotient(liquidity * factor.numerator, factor.denominator, amount_round) for factor in factors]
    out.update(liquidity=str(liquidity), sqrtPriceX96=str(current), tick_current=current_tick,
               tick_lower=lower_tick, tick_upper=upper_tick, round=mode, amount_round=amount_round,
               price_source="supplied_sqrt_price" if exact_price else "tick_boundary",
               scope="principal_only", amount_unit="raw")
    if "decimals0" in params or "decimals1" in params:
        decimal_scale(params)
    for index, amount in enumerate(amounts):
        out[f"amount{index}"] = str(amount)
        if f"decimals{index}" in params:
            out[f"amount{index}_readable"] = formatted(amount, params[f"decimals{index}"])
        if budgets is not None:
            difference = budgets[index] - amount
            out[f"leftover{index}"] = str(max(0, difference))
            out[f"shortfall{index}"] = str(max(0, -difference))
    if budgets is not None:
        out["within_budget"] = all(amount <= budget for amount, budget in zip(amounts, budgets))
    return out
