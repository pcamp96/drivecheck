"""Explicitly enable LAN listening while preserving other station settings."""

import os
import re
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
content = path.read_text()
pattern = r"^(?:export\s+)?DRIVECHECK_HOST\s*=.*$"
if re.search(pattern, content, re.MULTILINE):
    content = re.sub(pattern, "DRIVECHECK_HOST=0.0.0.0", content, flags=re.MULTILINE)
else:
    content = content.rstrip("\n") + "\nDRIVECHECK_HOST=0.0.0.0\n"
temporary = None
try:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
