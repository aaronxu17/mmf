import sys

__version__ = "1.0.0rc5"

msg = "MMF is only compatible with Python 3.6 and newer."


if sys.version_info < (3, 6):
    raise ImportError(msg)
