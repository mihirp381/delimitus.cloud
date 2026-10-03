"""The proof run's Streamlit app (T7, T9): the bake-off's pandas workload, so cold starts compare
with the bake-off's 22 s. Deployed with ``ssc deploy``; the build detects Streamlit, so it runs as
a session app (instance-billed, one instance at most)."""

import time

import numpy as np
import pandas as pd
import streamlit as st

st.title("SSC proof run: Streamlit + pandas")

t0 = time.perf_counter()
rng = np.random.default_rng(0)
df = pd.DataFrame(
    {
        "region": rng.choice(["north", "south", "east", "west"], 100_000),
        "amount": rng.normal(100, 25, 100_000),
        "qty": rng.integers(1, 20, 100_000),
    }
)
st.write(f"rows: {len(df):,}, built in {(time.perf_counter() - t0) * 1000:.0f} ms")
st.dataframe(df.groupby("region").agg(amount=("amount", "sum"), qty=("qty", "sum")))
st.caption(f"page served at {time.strftime('%H:%M:%S')} UTC")
