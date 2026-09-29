"""Access snapshots (SSC-021, decision 019): :mod:`.compiler` reads and publishes an org's
``ssc-snapshot/v1``, :mod:`.service` marks it dirty and records cell acknowledgements, and
:mod:`.jobs` is the worker task. The API imports :mod:`.service` only."""
