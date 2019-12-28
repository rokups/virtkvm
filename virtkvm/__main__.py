#!/usr/bin/env python3
import sys
from virtkvm import main

try:
    main()
except KeyboardInterrupt:
    pass
except Exception as e:
    print(str(e), file=sys.stderr)
