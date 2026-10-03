# Churn cohort explorer

Point `WAREHOUSE_DSN` at the analytics warehouse. Reads `fct_subscription_events`
and `dim_customer` from the `analytics` schema — these are dbt models owned by
the data team.
