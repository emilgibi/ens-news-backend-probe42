from openai import AzureOpenAI
from dotenv import load_dotenv
load_dotenv()
import httpx
from openai import OpenAI
from utils.db_utils import *


azure_endpoint = os.getenv('AZURE_ENDPOINT')
api_key = os.getenv('API_KEY')
CONFIG_TYPE = os.getenv('CONFIG')

# FOR LOCAL TESTING - CHANGE TO TRUE FOR FASTER PROMPT RESPONSE gpt-4-32k !!! ONLY IF REQUIRED
require_llm_response_speed = True
if require_llm_response_speed or (CONFIG_TYPE.lower() == "demo"):
    model_deployment_name = "gpt-4o-mini" #"ens-dev-gpt-4.1"
else:
    model_deployment_name = "gpt-4o"


# Sanity check (optional but recommended)
assert os.getenv("OPENAI_API_KEY"), "OPENAI_API_KEY not set"

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


client = OpenAI(
    http_client=http_client
)

# OpenAI
client1 = AzureOpenAI(
    azure_endpoint=azure_endpoint,
    api_key=api_key,
    api_version="2024-07-01-preview"
)