import os

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_333_API_KEY")

LLM_MODEL_PLANNER = os.getenv("LLM_MODEL_PLANNER")
LLM_MODEL_RESEARCHER = os.getenv("LLM_MODEL_RESEARCHER")
LLM_MODEL_SYNTHESIZER = os.getenv("LLM_MODEL_SYTHESIZER")