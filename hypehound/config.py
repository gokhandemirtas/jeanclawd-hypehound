from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    resend_api_key: str
    resend_inbox_id: str
    # OpenAI-compatible inference endpoint — works with LM Studio, Ollama, vLLM, or any remote provider
    inference_base_url: str = "http://localhost:1234/v1"  # LM Studio default
    inference_model: str = "local-model"
    db_path: str = "hypehound.db"
    digest_hour: int = 7  # daily digest generation time (local hour)
    voicebox_url: str = "http://localhost:8000"
    tts_voice_single: str = "aiden"   # Qwen3-TTS custom voice name
    tts_voice_male: str = "aiden"      # AIDEN in dual mode
    tts_voice_female: str = "vivian"   # VIVIAN in dual mode
    podcast_intro: str = "intro-v1.m4a"  # prepended as-is to every podcast; set to "" to disable

    model_config = {"env_file": ".env"}


settings = Settings()
