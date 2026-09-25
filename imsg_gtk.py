#!/usr/bin/env python3
"""GTK client. Run `./imsg_gtk.py [--debug]`."""
import sys

from mapmsg.gtk_app import run

if __name__ == "__main__":
    sys.exit(run())
