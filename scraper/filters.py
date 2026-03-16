"""
Filtering logic: diesel models, market priority, steering selection.
All functions are pure (no browser dependency).
"""

import re


# ------------------------------------------------------------------ #
# Diesel filter                                                        #
# ------------------------------------------------------------------ #

# Pattern captures common diesel model name endings:
#   316d, 318d, 320d, 325d, 330d, 335d, 520d, 525d ...
#   320xd, 330xd (x-drive diesel)
#   320d ed  (efficient dynamics variant of diesel)
#   316td, 318td (touring diesel)
_DIESEL_RE = re.compile(
    r"""
    ^\d+        # starts with digits (displacement digits)
    [a-z]*      # optional letters before 'd'
    d           # the 'd' that marks diesel
    (           # followed by:
      xd?       #   xd or x (all-wheel diesel)
    | \s        #   a space (e.g. "320d ed")
    | $         #   end of string
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_diesel(model_name: str) -> bool:
    """
    Return True if model_name represents a diesel variant.

    Examples that return True:
      316d, 318d, 320d, 325d, 330d, 335d
      320xd, 330xd
      320d ed
      318td, 316td

    Examples that return False (petrol / keep):
      316i, 318i, 320i, 323i, 325i, 328i, 330i, 335i
      316e, 318e, 320e  (hybrid)
      M3, M4, M5
      316ti, 318ti
      ActiveHybrid 5
    """
    name = model_name.strip()
    return bool(_DIESEL_RE.match(name))


# ------------------------------------------------------------------ #
# Market selection                                                     #
# ------------------------------------------------------------------ #

def select_market(available_markets: list) -> str | None:
    """
    Select the best available market.
    Priority: EGY > EUR > skip (return None).

    available_markets — list of market code strings, e.g. ["EUR", "USA", "EGY"]
    """
    for preferred in ("EGY", "EUR"):
        if preferred in available_markets:
            return preferred
    return None  # Neither EGY nor EUR available — skip this model


# ------------------------------------------------------------------ #
# Steering selection                                                   #
# ------------------------------------------------------------------ #

def select_steering(available_steerings: list) -> str | None:
    """
    Prefer Left hand drive.
    - If 'Left hand drive' is in the list → return it.
    - If only other options → return the first one.
    - If list is empty → return None (no steering option shown).
    """
    if not available_steerings:
        return None
    for opt in available_steerings:
        if "left" in opt.lower():
            return opt
    # Fall back to first available option (e.g. only Right hand drive)
    return available_steerings[0]
