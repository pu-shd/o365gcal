"""O365GCal reference sync engine.

This package is the *executable specification* for the one-way Outlook -> Google
Calendar mirror shipped as a Power Platform solution.

Power Automate flows cannot be run locally, so the decision logic they encode --
normalisation, content hashing, the create/update/delete diff, the throttle cap and
the mass-delete circuit breaker -- lives here in testable form. `expressions.py`
emits the Power Automate expression strings from these same rules so the tested
logic and the shipped logic cannot drift apart.
"""

__version__ = "1.0.0"
