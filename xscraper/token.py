"""Token for the public tweet-embed endpoint (cdn.syndication.twimg.com).

X's embed widget computes ``((id / 1e15) * Math.PI).toString(36)`` and strips
zeros and the dot. Python has no float-to-base-36 conversion, so this is a
port of V8's ``DoubleToRadixCString`` that produces the same digits.
"""
from __future__ import annotations

import math
import re

_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def js_float_to_radix(value: float, radix: int = 36) -> str:
    """Return what JavaScript's ``Number.prototype.toString(radix)`` returns."""
    if not 2 <= radix <= 36:
        raise ValueError("radix must be between 2 and 36")
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    negative = value < 0
    value = -value if negative else value

    integer = math.floor(value)
    fraction = value - integer
    delta = max(0.5 * (math.nextafter(value, math.inf) - value), math.nextafter(0.0, 1.0))

    frac_digits: list[str] = []
    if fraction >= delta:
        while True:
            fraction *= radix
            delta *= radix
            digit = int(fraction)
            frac_digits.append(_DIGITS[digit])
            fraction -= digit
            if (fraction > 0.5 or (fraction == 0.5 and digit & 1)) and fraction + delta > 1:
                # Round up, propagating the carry leftwards.
                while True:
                    if not frac_digits:
                        integer += 1
                        break
                    d = _DIGITS.index(frac_digits.pop())
                    if d + 1 < radix:
                        frac_digits.append(_DIGITS[d + 1])
                        break
                break
            if fraction < delta:
                break

    int_digits: list[str] = []
    n = int(integer)
    while True:
        n, rem = divmod(n, radix)
        int_digits.append(_DIGITS[rem])
        if n == 0:
            break

    out = "".join(reversed(int_digits))
    if frac_digits:
        out += "." + "".join(frac_digits)
    return "-" + out if negative else out


def syndication_token(tweet_id: int | str) -> str:
    return re.sub(r"(0+|\.)", "", js_float_to_radix((float(int(tweet_id)) / 1e15) * math.pi, 36))
