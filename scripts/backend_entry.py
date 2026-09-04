"""Frozen backend entry point. Desktop app starts and stops this child process."""
from multiprocessing import freeze_support
from greatsage.__main__ import main

if __name__ == "__main__":
    freeze_support()
    main()
