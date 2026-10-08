"""The second migration of the GA-4.5 fixture. The kit leaves it out of the first release."""

revision = "0002_ga45_second"
down_revision = "0001_ga45_first"

UP = "create table ga45_second (id serial primary key);"
