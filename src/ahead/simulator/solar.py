"""A shared sun position and irradiance calculation for building surfaces."""

from calendar import isleap
from datetime import datetime, timezone
from math import cos, pi, radians, sin


def sun_vector(moment: datetime, latitude: float, longitude: float) -> tuple[float, float, float]:
    """Return the sun direction as east, north, and up components."""
    utc = moment.astimezone(timezone.utc)
    hour = utc.hour + utc.minute / 60 + utc.second / 3600
    year_angle = 2 * pi / (366 if isleap(utc.year) else 365) * (
        utc.timetuple().tm_yday - 1 + (hour - 12) / 24
    )
    y = year_angle
    # NOAA fractional-year approximation.
    equation_minutes = 229.18 * (
        0.000075 + 0.001868 * cos(y) - 0.032077 * sin(y)
        - 0.014615 * cos(2 * y) - 0.040849 * sin(2 * y)
    )
    declination = (
        0.006918 - 0.399912 * cos(y) + 0.070257 * sin(y)
        - 0.006758 * cos(2 * y) + 0.000907 * sin(2 * y)
        - 0.002697 * cos(3 * y) + 0.00148 * sin(3 * y)
    )
    angle = radians((hour * 60 + equation_minutes + 4 * longitude) / 4 - 180)
    lat = radians(latitude)
    return (
        -cos(declination) * sin(angle),
        cos(lat) * sin(declination) - sin(lat) * cos(declination) * cos(angle),
        sin(lat) * sin(declination) + cos(lat) * cos(declination) * cos(angle),
    )


def incident_kw_m2(sun, azimuth_deg, tilt_deg, direct_kw_m2, diffuse_kw_m2, shade):
    if sun[2] <= 0:
        return 0.0
    azimuth = radians(azimuth_deg)
    tilt = radians(tilt_deg)
    beam = max(0, (
        sun[0] * sin(azimuth) * sin(tilt) + sun[1] * cos(azimuth) * sin(tilt)
        + sun[2] * cos(tilt)
    ))
    sky = (1 + cos(tilt)) / 2
    return direct_kw_m2 * beam * (1 - shade) + diffuse_kw_m2 * sky
