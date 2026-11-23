"""Lets Atlas's unchanged backend.py talk to any OpenAI-compatible provider (e.g. Groq)."""
try:
    import langchain_deepseek

    _Original = langchain_deepseek.ChatDeepSeek

    def _compat_chat_deepseek(*args, **kwargs):
        if "deepseek.com" not in str(kwargs.get("api_base", "")):
            kwargs.pop("extra_body", None)
        return _Original(*args, **kwargs)

    langchain_deepseek.ChatDeepSeek = _compat_chat_deepseek
except ImportError:
    pass
