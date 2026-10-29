from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings. Override any of them with env vars or a .env file,
    e.g. ASYNCCOIN_CACHE_TTL_SECONDS=30."""

    model_config = SettingsConfigDict(env_prefix="PWRAL_", env_file=".env", extra="ignore")

    groq_333_api_key: str 

    llm_model_planner: str
    llm_model_researcher: str
    llm_model_sythesizer: str

    database_url: str

    # --- Auth ---
    jwt_secret_key: str  # generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    api_key_encryption_key: str

    # --- Email verification (Brevo) ---
    # If brevo_api_key is empty, the verification link is logged instead of emailed (dev mode).
    brevo_api_key: str
    brevo_sender_email: str = ""  # must be a sender/domain verified in Brevo
    brevo_sender_name: str = "Personal Web Research Agent"

    app_base_url: str   # Meaning the backend

    # Frontend page that receives ?token=... and POSTs it to /api/auth/verify-email
    frontend_base_url: str = "" # Not there
    #frontend_verify_url: str = frontend_base_url + "#/verify" if frontend_base_url else "http://localhost:5500"
    verification_token_ttl_hours: int = 24
    verification_resend_cooldown_seconds: int = 600
    frontend_path: str = ""

    password_reset_token_ttl_minutes: int = 60


settings = Settings()