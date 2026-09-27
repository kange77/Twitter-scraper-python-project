import sys

from .cli import main

if __name__ == "__main__":  # crawl --processes re-imports the main module in each helper
    sys.exit(main())
