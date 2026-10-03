import streamlit as st

st.title("Hello")
name = st.text_input("Your name")
st.write(f"Hi {name}")
