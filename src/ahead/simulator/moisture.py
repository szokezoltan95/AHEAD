"""Single-node room moisture model for cooling and dehumidification."""

from math import exp

AIR_KWH_KG_K = 1.005 / 3600
LATENT_KWH_KG = 0.68


def humidity_ratio(temperature_c, relative_humidity):
    """Approximate kg of vapour per kg of dry air at standard pressure."""
    temperature_c = max(-70, min(65, temperature_c))
    vapour_kpa = relative_humidity / 100 * 0.6108 * exp(
        17.27 * temperature_c / (temperature_c + 237.3)
    )
    return 0.622 * vapour_kpa / (101.325 - vapour_kpa)


def relative_humidity(temperature_c, ratio):
    saturation = humidity_ratio(temperature_c, 100)
    vapour = ratio / (0.622 + ratio)
    return min(100.0, max(0.0, 100 * vapour * (0.622 + saturation) / saturation))
