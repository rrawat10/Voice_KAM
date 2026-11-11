from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    gemini_api_key: str = ''
    live_model: str = 'gemini-3.8-live'
    planner_model: str = 'gemini-3.5-flash-lite'
    planner_thinking_level: str = 'minimal'
    session_ttl_seconds: int = 1800
    max_call_seconds: int = 300
    max_concurrent_calls: int = 2
    planner_timeout_seconds: float = 8
    secure_cookies: bool = False


settings = Settings()
