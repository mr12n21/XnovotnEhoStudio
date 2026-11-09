import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("NSS_HOME", ROOT))

LIBRARY_DIR = HOME / "library"
EXPORT_DIR = HOME / "exports"
SESSION_FILE = HOME / "session.json"

SAMPLE_RATE = 44100
MAX_SOUND_SECONDS = 90          # delší zvuky se při načtení oříznou
BACKEND = os.environ.get("NSS_BACKEND", "auto")  # auto | sd | qt | null

USER_AGENT = "NovotnehoSoundStudio/0.1 (personal loop mixer; python-urllib)"
REQUEST_DELAY = 0.4             # pauza mezi stránkami (slušnost vůči serveru)
