from waitress import serve
from flask import Flask, request, jsonify
import extraction_model
import json
import logging
from logging.handlers import RotatingFileHandler

from logging.config import dictConfig

# Configure rotating file logging
max_bytes = 1024 * 1024 * 5  # 5 MB
backup_count = 10  # Keep 10 backup logs
formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")

file_handler = RotatingFileHandler("app.log", maxBytes=max_bytes, backupCount=backup_count)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)  # Set desired log level

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)  # Set desired log level

# Add both handlers to the root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

app = Flask(__name__)

extraction_model.init()

@app.route('/', methods = ['GET', 'POST'])
def run():
    app.logger.info('Extraction Module Called')
    try:
        print(request.json)
        result = extraction_model.run(json.dumps(request.json), app.logger)
    except Exception as e:
        app.logger.info(str(e))
        template = "An exception of type {0} occurred. Arguments:\n{1!r}"
        resp = template.format(type(e).__name__, e.args)
        return resp, 500

    app.logger.info('execution completed')
    return jsonify(result)

@app.route('/ready', methods = ['GET'])
def ready():
    return jsonify("Ok")


@app.route('/health', methods = ['GET'])
def health():
    return jsonify("Ok")


serve(app, host="0.0.0.0", port=8081, threads=10)  # TODO
