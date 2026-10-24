"""Color themes the director can pick per tournament. The colors live in static/style.css
([data-theme="..."] blocks); this list is the menu, and the swatches shown next to it."""

DEFAULT_THEME = "electric"

# key, name, group, swatches (main accent, second color, background)
THEMES = [
    ("electric", "Electric blue", "Everyday", ("#38b6ff", "#1a6dff", "#030916")),
    ("neutral", "Neutral", "Everyday", ("#d8c3a0", "#8c7a62", "#0e0d0c")),
    ("mustard", "Mustard yellow", "Everyday", ("#e2ad1f", "#c98a0c", "#0f0c07")),
    ("neon", "Neon green", "Everyday", ("#39ff88", "#00c853", "#030b06")),
    ("newyear", "New Year's", "Holidays", ("#f2c14e", "#d9dde3", "#08080a")),
    ("valentines", "Valentine's Day", "Holidays", ("#ff5c9d", "#ff2d55", "#13050c")),
    ("mardigras", "Mardi Gras", "Holidays", ("#b46cff", "#f2c14e", "#0c0614")),
    ("stpatricks", "St. Patrick's Day", "Holidays", ("#2fd06f", "#f2c14e", "#04100a")),
    ("easter", "Easter / spring", "Holidays", ("#c6a6ff", "#ffd1e6", "#0f0b16")),
    ("patriotic", "Patriotic (Memorial Day, July 4th, Labor Day, Veterans Day)", "Holidays", ("#4a86ff", "#e63946", "#050a1a")),
    ("halloween", "Halloween", "Holidays", ("#ff7a1a", "#9b5cff", "#0b0710")),
    ("thanksgiving", "Thanksgiving / fall", "Holidays", ("#e8862a", "#a3283c", "#120a06")),
    ("christmas", "Christmas", "Holidays", ("#e0384b", "#1f9d55", "#06110b")),
    ("hanukkah", "Hanukkah", "Holidays", ("#5b94ff", "#dfe6f2", "#050a18")),
]
THEME_KEYS = {key for key, *_ in THEMES}


def theme_key(value: str | None) -> str:
    return value if value in THEME_KEYS else DEFAULT_THEME
