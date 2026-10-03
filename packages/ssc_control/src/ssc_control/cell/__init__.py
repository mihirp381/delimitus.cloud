"""Lazy cell resources (SSC-087, decision 022 amendment pending): a cell's database, egress
proxy and data gateway appear the first time an app needs them, with no person running a command.

``resources`` asks for one (the deploy, approval and admin triggers) and holds a deployment until
it exists; ``tasks`` names the worker task and defers it; ``create`` is one step of the job, which
starts and watches the cell deployer; ``deployer`` is how the worker reaches that job, with two
arguments and no other power; ``jobs`` is the worker's blueprint.
"""
