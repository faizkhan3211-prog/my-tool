import re
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Lead List Cleaner")
st.title("Lead List Cleaner")
st.caption("Built by Faiz Khan")

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")

file = st.file_uploader("CSV upload karo", type="csv")

if file:
    df = pd.read_csv(file)
    st.write("Columns:", list(df.columns))
    col = st.selectbox("Email column select karo", df.columns)

    if st.button("Clean karo"):
        before = len(df)
        df[col] = df[col].astype(str).str.strip().str.lower()
        df["valid_email"] = df[col].apply(lambda x: bool(EMAIL_RE.match(x)))
        df = df[df["valid_email"]].drop_duplicates(subset=col)
        st.success(f"{before} rows mein se {len(df)} clean rows bache")
        st.dataframe(df)
        st.download_button("Download clean CSV",
                           df.to_csv(index=False),
                           "clean_leads.csv")
