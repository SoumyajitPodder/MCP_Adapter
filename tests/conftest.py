import os

from hypothesis import settings

pytest_plugins = ["tests.sentinels", "pytester"]

# "ci" is thorough and reproducible; "dev" keeps the local loop fast. CI sets HYPOTHESIS_PROFILE=ci.
settings.register_profile("ci", max_examples=500, derandomize=True, deadline=None)
settings.register_profile("dev", max_examples=50)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
