from dataclasses import dataclass
from typing import Union

@dataclass
class PllClock:
    """Phase-locked loop clock output
    
    frequency: Output frequency in Hz
    tolerance: Maximum relative error
    """

    frequency: Union[float, int]
    tolerance: Union[float, int] = 0.001


