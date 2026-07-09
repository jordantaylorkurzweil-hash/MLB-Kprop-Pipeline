"""park_coords.py — lat/long for each MLB park, keyed by home team abbreviation.

Used only to query a free weather API (Open-Meteo, no key required) for
temperature / wind / precipitation probability at game time. Public,
non-copyrighted geographic data.
"""

PARK_COORDS = {
    "ARI": (33.4455, -112.0667),
    "ATL": (33.8908, -84.4678),
    "ATH": (38.5806, -121.5136),   # Sutter Health Park, Sacramento
    "BAL": (39.2838, -76.6217),
    "BOS": (42.3467, -71.0972),
    "CHC": (41.9484, -87.6553),
    "CWS": (41.8299, -87.6338),
    "CIN": (39.0975, -84.5061),
    "CLE": (41.4962, -81.6852),
    "COL": (39.7559, -104.9942),
    "DET": (42.3390, -83.0485),
    "HOU": (29.7573, -95.3555),     # Daikin Park (retractable roof)
    "KCR": (39.0517, -94.4803),
    "LAA": (33.8003, -117.8827),
    "LAD": (34.0739, -118.2400),
    "MIA": (25.7781, -80.2196),     # loanDepot park (retractable roof)
    "MIL": (43.0280, -87.9712),     # American Family Field (retractable roof)
    "MIN": (44.9817, -93.2776),
    "NYM": (40.7571, -73.8458),
    "NYY": (40.8296, -73.9262),
    "OAK": (38.5806, -121.5136),    # alias for ATH
    "PHI": (39.9061, -75.1665),
    "PIT": (40.4469, -80.0057),
    "SDP": (32.7073, -117.1566),
    "SEA": (47.5914, -122.3325),    # T-Mobile Park (retractable roof)
    "SFG": (37.7786, -122.3893),
    "STL": (38.6226, -90.1928),
    "TBR": (27.7683, -82.6534),     # domed
    "TEX": (32.7473, -97.0842),     # retractable roof
    "TOR": (43.6414, -79.3894),     # retractable roof
    "WSN": (38.8730, -77.0074),
}

# Parks with a fixed or retractable roof — PPD% forced low regardless of forecast
# unless the game feed itself reports a delay/postponement.
INDOOR_OR_RETRACTABLE = {
    "ATH", "OAK", "HOU", "MIA", "MIL", "SEA", "TBR", "TEX", "TOR", "ARI",
}
