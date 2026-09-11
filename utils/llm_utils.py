from openai import AzureOpenAI
from dotenv import load_dotenv
load_dotenv()
import httpx
from utils.db_utils import *


# Kept for other modules that pull this in via `from .llm_analysis import
# require_llm_response_speed` (e.g. models/item_model.py) — no longer used
# for model selection here (see model_deployment_name below), but removing
# the name outright would break that import.
require_llm_response_speed = True

# Azure OpenAI — same OPENAI__* env var convention and gpt-5.1 deployment as
# ens-orchestration-probe42's _call_openai(), replacing the public OpenAI
# client (OPENAI_API_KEY) every call site in this project used to go
# through. client1's separate, unused AzureOpenAI construction is gone —
# this is now the one and only client, and it's already Azure.
azure_endpoint = os.getenv('OPENAI__AZURE_ENDPOINT')
api_key = os.getenv('OPENAI__API_KEY')
model_deployment_name = os.getenv('OPENAI__MODEL_DEPLOYMENT_NAME', 'gpt-5.1')

assert azure_endpoint and api_key, "OPENAI__AZURE_ENDPOINT / OPENAI__API_KEY not set"

# --------------------------------------------------
# Custom HTTPX client (corporate / proxy safe)
# --------------------------------------------------
http_client = httpx.Client(
    verify=False,              # ⚠️ Disable SSL verification (corporate proxy)
    timeout=60.0,
    limits=httpx.Limits(
        max_keepalive_connections=1,
        max_connections=2
    )
)


client = AzureOpenAI(
    azure_endpoint=azure_endpoint,
    api_key=api_key,
    api_version="2024-07-01-preview",
    http_client=http_client,
)
