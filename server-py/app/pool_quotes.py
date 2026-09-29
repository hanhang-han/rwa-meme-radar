"""Convert block-pinned AMM state into a marginal exchange ratio, never a USD price."""
from decimal import Decimal, localcontext


def pool_ratio(token0, meme, decimals0, decimals1, *, reserves=None, sqrt_price_x96=None):
    if not all(isinstance(d, int) and 0 <= d <= 255 for d in (decimals0, decimals1)):
        return None
    with localcontext() as ctx:
        ctx.prec = 90
        if reserves and all(r > 0 for r in reserves):
            price1_per0 = Decimal(reserves[1]) / Decimal(reserves[0])
        elif sqrt_price_x96 and sqrt_price_x96 > 0:
            price1_per0 = (Decimal(sqrt_price_x96) / Decimal(2**96))**2
        else:
            return None
        price1_per0 *= Decimal(10) ** (decimals0 - decimals1)
        # Meme per stock-side unit: invert only when the meme is token0.
        ratio = 1 / price1_per0 if token0.lower() == meme.lower() else price1_per0
        result = float(ratio)
        return result if 0 < result < float("inf") else None
