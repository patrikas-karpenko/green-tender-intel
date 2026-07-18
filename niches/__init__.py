import importlib

def get_niche(name):
    try:
        module = importlib.import_module(f"niches.{name}")
    except ModuleNotFoundError:
        raise SystemExit(f"Unknown niche '{name}'. Create niches/{name}.py.")
    return module.NICHE