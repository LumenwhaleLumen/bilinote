import sys
from pathlib import Path

# 让 tests/ 能 import 仓库根目录的 bilinote
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
