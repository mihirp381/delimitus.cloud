"""An app's life after it ships (SSC-025): the admin's inventory and the kill switch.

:mod:`.inventory` lists every app with its owner, environments, sharing and last use.
:mod:`.kill_switch` stops an app in a fixed order and puts it back; :mod:`.jobs` runs the saga
in the worker, and the API defers it by name through :mod:`.tasks`.
"""
