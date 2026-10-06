"""Automated UI tests must not take focus from the user's windows."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
