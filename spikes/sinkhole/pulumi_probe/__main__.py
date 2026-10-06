import pulumi

import program

config = pulumi.Config()
program.build(
    config.get("project") or program.PROJECT,
    config.get_int("count") or program.DEFAULT_COUNT,
)
