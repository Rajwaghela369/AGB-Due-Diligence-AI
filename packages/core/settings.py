"""Runtime settings: secrets and deployment specifics, read from the environment.

Kept strictly separate from the YAML configs, which are versioned inputs to the
pipelines and are hashed into the store manifest.
"""

from pydantic import AliasChoices, Field
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
    auth_enabled: bool | None = Field(default=None, alias="AGB_AUTH_ENABLED")

    # Managed Neon Auth OIDC values. These are deployment configuration, not app tables
    # or credentials; the Neon Auth service owns its `neon_auth` schema.
    neon_auth_issuer: str = Field(default="", alias="NEON_AUTH_ISSUER")
    neon_auth_audience: str = Field(default="", alias="NEON_AUTH_AUDIENCE")
    neon_auth_jwks_uri: str = Field(default="", alias="NEON_AUTH_JWKS_URI")

    # Empty means anonymous access, which is enough for a public dataset.
    hf_token: str = Field(default="", alias="HF_TOKEN")

    langfuse_public_key: str = Field(default="", alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_HOST")

    # Object-storage credentials are deployment secrets, deliberately separate from the
    # provider-neutral endpoint and bucket in configs/app.yaml.
    # Standard AWS names are primary; S3_* remains supported for existing deployments.
    s3_access_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID"),
    )
    s3_secret_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY"),
    )
    aws_endpoint_url_s3: str = Field(default="", alias="AWS_ENDPOINT_URL_S3")
    aws_region: str = Field(default="", alias="AWS_REGION")


def load_settings() -> Settings:
    return Settings()
