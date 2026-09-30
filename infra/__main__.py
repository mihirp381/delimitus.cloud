import pulumi

from ssc_infra import cell, naming, platform

stack = pulumi.get_stack()
if stack == naming.PLATFORM_STACK:
    platform.build()
else:
    cell.build(stack)
