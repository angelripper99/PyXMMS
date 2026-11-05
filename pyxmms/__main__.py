"""Start PyXMMS when the package is run with python -m pyxmms.

Copyright (C) 2026 the PyXMMS authors. Released under GPL-2.0-or-later. See LICENSE.
"""

import sys

from .app import main

sys.exit(main())
