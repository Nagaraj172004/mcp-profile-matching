import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("USE_LLM", "0")
