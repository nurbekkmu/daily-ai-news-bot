"""
Entry point for manual/scheduled digest runs.

Run manually with:  python main.py
Also runnable via .github/workflows/daily.yml ("Run workflow" button).
The actual pipeline lives in pipeline.py, shared with poll_telegram.py.
"""

import logging

# Load environment variables FIRST — config.py reads os.environ at import time,
# so .env must be loaded before config (and anything importing it) is imported.
from dotenv import load_dotenv
load_dotenv()

import pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


if __name__ == "__main__":
    pipeline.run()
