import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Import the Flask app from src.app. This is the correct runtime entrypoint
# for the repo, and it points static files at the actual assets directory.
from src.app import app

# Match the actual asset location in this repo: src/public
app.static_folder = os.path.join(ROOT_DIR, "src", "public")


from flask import send_from_directory  # noqa: E402


@app.route("/<any(css,js,img):folder>/<path:filename>")
def trimzee_assets(folder, filename):
    return send_from_directory(os.path.join(SRC_DIR, "public", folder), filename)
