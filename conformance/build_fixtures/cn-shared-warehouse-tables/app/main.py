import os
import streamlit as st
from sqlalchemy import create_engine, text

# Shared analytics warehouse. This artifact is one of several consumers; the
# schema is produced upstream by dbt and this app has no part in creating it.
engine = create_engine(os.environ["WAREHOUSE_DSN"])

st.title("Churn cohorts")

# `analytics.fct_subscription_events` and `analytics.dim_customer` are never
# created here. There is no migration, no CREATE TABLE, no dbt project in this
# repo — only SELECTs against a schema that must already exist.
QUERY = text(
    """
    select date_trunc('month', c.signed_up_at) as cohort,
           count(distinct e.customer_id) as churned
      from analytics.fct_subscription_events e
      join analytics.dim_customer c on c.customer_id = e.customer_id
     where e.event_type = 'cancelled'
     group by 1 order by 1
    """
)

with engine.connect() as conn:
    st.dataframe(conn.execute(QUERY).fetchall())
