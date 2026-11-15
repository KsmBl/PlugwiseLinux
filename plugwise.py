#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the CLI from a source checkout without installing: ./plugwise.py ..."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "src"))
from plugwise_stick.cli import main  # noqa: E402

sys.exit(main())
