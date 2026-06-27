"""Plotly Dash application initialization."""

import dash
import dash_bootstrap_components as dbc

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.DARKLY,
        dbc.icons.FONT_AWESOME,  # kept — legacy fa- classes across the app
        # Phosphor Icons — used by Drishti v2 for refreshed iconography
        "https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/style.css",
        "https://unpkg.com/@phosphor-icons/web@2.1.1/src/fill/style.css",
        "https://unpkg.com/@phosphor-icons/web@2.1.1/src/duotone/style.css",
        # Inter (body) + JetBrains Mono (numbers) + Space Grotesk (display)
        "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap",
    ],
    suppress_callback_exceptions=True,
    title="Drishti — Road to 10M",
    update_title="Scanning...",
)

server = app.server
