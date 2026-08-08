import json
import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


def _load(fname):
    with open(os.path.join(DATA_DIR, fname)) as f:
        return json.load(f)


class Settings:
    def __init__(self):
        self.model_pricing = _load("model_pricing.json")
        self.gateway_config = _load("gateway_config.sample.json")
        self.seed_keys = _load("seed_keys.json")

    def reload(self):
        self.__init__()


settings = Settings()
