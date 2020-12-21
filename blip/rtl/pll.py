from dataclasses import dataclass
from typing import Union, Tuple

@dataclass
class PllClock:
    """Phase-locked loop clock output
    
    frequency: Output frequency in Hz
    tolerance: Maximum relative error, either a single value or (-, +)
    """

    frequency: float
    tolerance: Union[float, Tuple[float, float]] = 0.001

    def tolerance_below(self):
        t = self.tolerance
        return t[0] if hasattr(t, "__getitem__") else -t
    def tolerance_above(self):
        t = self.tolerance
        return t[1] if hasattr(t, "__getitem__") else t
