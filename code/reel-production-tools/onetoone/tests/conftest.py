"""Put tools/ on the path so `from onetoone import grade` works from this subdirectory."""
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
