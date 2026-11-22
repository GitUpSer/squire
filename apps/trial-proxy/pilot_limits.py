"""Bound in-flight spend without replacing LiteLLM's team budget accounting."""
from litellm.integrations.custom_logger import CustomLogger


class PilotLimits(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # Applies to both OpenAI-shaped and native Anthropic requests, before
        # the upstream call. This is a maximum, not a default supplied to clients.
        if call_type in {"completion", "acompletion", "anthropic_messages", "pass_through_endpoint"}:
            for field in ("max_tokens", "max_completion_tokens"):
                if field in data:
                    data[field] = min(int(data[field]), 8192)
            if "max_tokens" not in data and "max_completion_tokens" not in data:
                data["max_tokens"] = 8192
            thinking = data.get("thinking")
            if isinstance(thinking, dict) and thinking.get("type") == "enabled":
                # Anthropic requires the thinking budget to be below max_tokens.
                limit = data.get("max_tokens", data.get("max_completion_tokens", 8192))
                if "budget_tokens" in thinking:
                    thinking["budget_tokens"] = min(int(thinking["budget_tokens"]), limit - 1)
        return data


pilot_limits = PilotLimits()
