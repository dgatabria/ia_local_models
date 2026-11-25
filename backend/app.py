import streamlit as st
import admin_ui
import auth
import chat_ui
import database as db

st.set_page_config(page_title="Qwen Chat", layout="wide")

db.ensure_tables()

user = auth.get_current_user()

if user is None:
    auth.render_login_form()
    st.stop()

if user["is_admin"]:
    admin_ui.render(user)
else:
    chat_ui.render(user)
