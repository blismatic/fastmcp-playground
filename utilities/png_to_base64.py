"""
This script converts a PNG image to a base64-encoded string and generates an HTML link tag for the favicon used in setup.html.
The generated HTML link tag can be directly inserted into the HTML file to include the favicon without needing an external image file.
"""

import base64
from pathlib import Path

FILE_PATH = Path(__file__).parent.parent / "assets" / "brand" / "favicon.png"

png_data = base64.b64encode(FILE_PATH.read_bytes()).decode("ascii")

html = f'<link rel="icon" type="image/png" href="data:image/png;base64,{png_data}">'

print(html)
