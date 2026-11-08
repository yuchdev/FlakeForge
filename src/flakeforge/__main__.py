"""Module entry point for ``python -m flakeforge``."""
import sys

from .cli import main

sys.exit(main())
