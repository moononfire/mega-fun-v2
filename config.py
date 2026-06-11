import os
from dotenv import load_dotenv

load_dotenv()

VPS_WORKER_TOKEN = os.environ["VPS_WORKER_TOKEN"]
CLIENTS_DIR = os.environ.get("CLIENTS_DIR", "/home/deploy/clients")
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/home/deploy/scripts")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/home/deploy/worker.db")
PORT = int(os.environ.get("PORT", 8001))
