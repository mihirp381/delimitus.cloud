-- SSC-015 · The session framework the build found in the source (Streamlit, Gradio, Dash, Shiny).
-- Kept on the build while it runs and copied to its release, where the runtime reads it to treat
-- the app as a session app. A release is immutable, so the column is written once, at insert.

ALTER TABLE ssc.build
  ADD COLUMN framework text CHECK (framework ~ '^[a-z][a-z0-9_-]{0,31}$');

ALTER TABLE ssc.release
  ADD COLUMN framework text CHECK (framework ~ '^[a-z][a-z0-9_-]{0,31}$');
