from typing import Final

PRINTABLE: Final = r"^[^\x00-\x1f\x7f]+$"
PRINTABLE_LINES: Final = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+$"
