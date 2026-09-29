"""Timers (SSC-041, decision 020): schedules declared in a release's manifest, fired by the
worker at their cron instants, and run by hand from the API.

``service`` keeps the schedule rows (``TimersPort`` for the deploy job and the kill switch, plus
pause, resume and manual runs for the API); ``tasks`` names the worker task and defers it;
``runner`` claims, dispatches and records one run; ``jobs`` is the worker's blueprint;
``dispatch`` is how a run reaches the app.
"""
