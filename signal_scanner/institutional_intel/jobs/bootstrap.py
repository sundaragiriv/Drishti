"""Bootstrap job for institutional intelligence layer."""

from signal_scanner.institutional_intel.warehouse.db import init_warehouse


def main() -> None:
    init_warehouse()


if __name__ == "__main__":
    main()

