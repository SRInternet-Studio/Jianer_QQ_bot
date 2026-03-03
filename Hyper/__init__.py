try:
    import hyperot as _hyperot
    HYPER_BOT_VERSION = getattr(_hyperot, "HYPER_BOT_VERSION", "0.0.0")
except Exception:
    HYPER_BOT_VERSION = "0.0.0"
