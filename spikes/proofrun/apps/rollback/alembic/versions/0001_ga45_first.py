"""The first migration of the GA-4.5 fixture. Read by the platform, never run."""

revision = "0001_ga45_first"
down_revision = None

UP = "create table ga45_first (id serial primary key);"
