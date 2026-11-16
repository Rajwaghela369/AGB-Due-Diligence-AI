"""Runtime settings: secrets and deployment specifics, read from the environment.

Kept strictly separate from the YAML configs, which are versioned inputs to the
pipelines and are hashed into the store manifest.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # Required by the agent: the judge and the explanation run on Gemini.
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    jina_api_key: str = Field(default="", alias="JINA_API_KEY")

    qdrant_url: str = Field(default="", alias="QDRANT_URL")
    qdrant_api_key: str = Field(default="", alias="QDRANT_API_KEY")

    # Neon Postgres. The connection string is a secret, so it never sits in a
    # YAML config; the schema that uses it is versioned code.
    database_url: str = Field(default="", alias="DATABASE_URL")

    # Empty means anonymous access, which is enough for a public dataset.
    hf_token: str = Field(default="", alias="HF_TOKEN")

    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")


def load_settings() -> Settings:
    return Settings()
