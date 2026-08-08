CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def safe_csv_value(value):
    if value is None:
        return ""
    text = str(value)
    if text.lstrip(" \t\r\n").startswith(CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def safe_csv_row(values):
    return [safe_csv_value(value) for value in values]
