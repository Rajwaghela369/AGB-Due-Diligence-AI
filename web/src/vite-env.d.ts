/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL?: string;
  // Base URL for "trace ↗" links in Technical details, e.g. https://cloud.langfuse.com/project/<id>
  readonly VITE_LANGFUSE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
