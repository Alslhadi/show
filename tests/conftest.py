import os

os.environ.setdefault("DASHBOARD_TOKEN", "t")
os.environ.setdefault("WEBHOOK_SECRET", "s")
os.environ.setdefault("STRATEGY_MODE", "SMC")  # legacy API tests exercise the SMC path on the paper broker
os.environ.setdefault("TG_EXECUTE", "true")
