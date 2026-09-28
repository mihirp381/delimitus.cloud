import json
import time

import numpy as np
import pandas as pd
import streamlit as st

import probes

st.title("SSC bake-off: Streamlit + pandas")

t0 = time.perf_counter()
rng = np.random.default_rng(0)
df = pd.DataFrame({
    "region": rng.choice(["north", "south", "east", "west"], 100_000),
    "amount": rng.normal(100, 25, 100_000),
    "qty": rng.integers(1, 20, 100_000),
})
st.write(f"rows: {len(df):,}, built in {(time.perf_counter() - t0) * 1000:.0f} ms")
st.dataframe(df.groupby("region").agg(amount=("amount", "sum"), qty=("qty", "sum")))

st.subheader("probes")
st.json({"egress": probes.egress(), "metadata": probes.metadata()})
st.caption("WebSocket origin check: open this app through the proxy host name. If the page "
           "stays on 'Please wait' or the browser console shows a 403 on /_stcore/stream, the "
           "origin check rejected the proxied host. Fix candidates: --server.enableXsrfProtection=false, "
           "--server.enableCORS=false, or forward Origin/Host unchanged. Record which was needed.")
