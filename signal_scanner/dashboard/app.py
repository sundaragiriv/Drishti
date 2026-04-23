"""Plotly Dash application initialization."""

import dash
import dash_bootstrap_components as dbc

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.DARKLY,
        dbc.icons.FONT_AWESOME,
        "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap",
    ],
    suppress_callback_exceptions=True,
    title="Kubera",
    update_title="Scanning...",
)

server = app.server
