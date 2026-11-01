def capitalize_first(text: str) -> str:
    """Capitalize a display label while preserving the rest of its spelling"""
    text = text.strip()
    return text[:1].upper() + text[1:]
