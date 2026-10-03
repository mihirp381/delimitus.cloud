from dash import Dash, html

app = Dash(__name__)
server = app.server
app.layout = html.Div([html.H1("Pipeline"), html.P("Open deals by stage.")])
