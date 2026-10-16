"""LangChain chat adapters; GreatSage owns credentials, metering and cancellation.

No SDK retry or remote tracing: task retry and local request auditing are explicit
application policies. The two small subclasses retain provider metadata which
LangChain otherwise omits; dependency versions are pinned and protocol-tested.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, aclosing

import httpx
import ollama
import openai
from langchain_core.messages import ToolMessage
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langsmith import tracing_context

from .providers import ProviderError, _generation_usage, _network_code, _number, _tool_calls, _usage


class MeteredChatOpenAI(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(self, chunk, *args, **kwargs):
        result = super()._convert_chunk_to_generation_chunk(chunk, *args, **kwargs)
        if result is not None and chunk.get("usage"):
            # Standard UsageMetadata omits OpenRouter's actual billed cost.
            result.message.response_metadata["greatsage_usage"] = _usage(chunk["usage"])
        return result


class NamedToolChatOllama(ChatOllama):
    def _convert_messages_to_ollama_messages(self, messages):
        result = super()._convert_messages_to_ollama_messages(messages)
        for source, destination in zip(messages, result):
            if isinstance(source, ToolMessage) and source.name:
                destination["tool_name"] = source.name
        return result


class SharedTransport(httpx.AsyncBaseTransport):
    """Borrow the desktop HTTP pool without giving SDKs ownership of it."""

    def __init__(self, client: httpx.AsyncClient, authorization: str | None):
        self.client, self.authorization = client, authorization
        self.responses: list[httpx.Response] = []

    async def handle_async_request(self, request):
        # Do not inherit the SDK's ambient account/project or placeholder key.
        for name in ("authorization", "openai-organization", "openai-project"):
            request.headers.pop(name, None)
        if self.authorization:
            request.headers["authorization"] = self.authorization
        response = await self.client.send(request, stream=True)
        self.responses.append(response)
        return response

    async def aclose(self):
        # LangChain has nested async generators. Close the actual responses now,
        # including when a consumer closes between two streamed tokens.
        for response in self.responses:
            await response.aclose()
        self.responses.clear()


def _no_sync_request(request):
    raise RuntimeError("GreatSage chat is asynchronous")


async def stream_chat(service, config: dict, messages: list):
    provider = str(config.get("provider", "")).lower()
    if provider not in ("openrouter", "openai", "openai_compatible", "ollama"):
        raise ProviderError(provider, "chat_not_supported")
    if not config.get("model"):
        raise ProviderError(provider, "model_missing")
    local = provider == "ollama"
    endpoint = "/api/chat" if local else "/chat/completions"
    url, headers, timeout = service._request(config, endpoint)
    base = url.removesuffix(endpoint)
    limit = config.get("max_tokens", config.get("max_output_tokens"))
    transport = SharedTransport(service._client, headers.get("Authorization"))
    finished, aggregate, call_size = False, None, 0
    try:
        # Explicitly disable inherited LangSmith export, even if the host has
        # tracing environment variables. GreatSage audit_content remains opt-in.
        with tracing_context(enabled=False):
            async with AsyncExitStack() as stack:
                stack.push_async_callback(transport.aclose)
                sync = stack.enter_context(httpx.Client(transport=httpx.MockTransport(_no_sync_request)))
                client = await stack.enter_async_context(httpx.AsyncClient(transport=transport, timeout=timeout))
                kwargs = {"model": config["model"], "base_url": base, "verbose": False, "cache": False}
                if "temperature" in config:
                    kwargs["temperature"] = config["temperature"]
                invoke = {}
                if local:
                    model = NamedToolChatOllama(
                        **kwargs, reasoning=False, num_predict=limit,
                        async_client_kwargs={"transport": transport, "timeout": timeout},
                        sync_client_kwargs={"transport": httpx.MockTransport(_no_sync_request)})
                    # LangChain has no public close method; close its two SDK
                    # clients using their public close methods (pinned adapter).
                    stack.callback(model._client.close)
                    stack.push_async_callback(model._async_client.close)
                    if config.get("json_schema"):
                        invoke["format"] = config["json_schema"]
                else:
                    model = MeteredChatOpenAI(
                        **kwargs, api_key=config.get("api_key") or "unused", max_tokens=limit,
                        max_retries=0, timeout=timeout, http_client=sync, http_async_client=client,
                        organization="", use_responses_api=False,
                        output_version="v0", stream_usage=config.get("include_usage", True))
                    if config.get("json_schema"):
                        # Keep raw streamed JSON for the existing schema validator;
                        # do not switch to SDK beta parsing / buffer the entire reply.
                        invoke["extra_body"] = {"response_format": {"type": "json_schema", "json_schema": {
                            "name": "greatsage_decision", "strict": True, "schema": config["json_schema"]}}}
                if config.get("tools"):
                    model = model.bind_tools(config["tools"], tool_choice="auto")
                async with asyncio.timeout(_number(config, "total_timeout_seconds", 120, .1, 600)):
                    async with aclosing(model.astream(messages, config={"callbacks": []}, **invoke)) as stream:
                        async for chunk in stream:
                            metadata = chunk.response_metadata
                            reason = metadata.get("finish_reason") or metadata.get("done_reason")
                            if reason in ("error", "content_filter", "load"):
                                raise ProviderError(provider, "generation_not_completed")
                            finished |= bool(metadata.get("finish_reason") or metadata.get("done"))
                            if isinstance(chunk.content, str) and chunk.content:
                                yield {"text": chunk.content}
                            usage = metadata.get("greatsage_usage")
                            if local and metadata.get("done"):
                                usage = {destination: metadata[source] for source, destination in (
                                    ("prompt_eval_count", "prompt_tokens"), ("eval_count", "completion_tokens"))
                                    if isinstance(metadata.get(source), int)}
                                if usage:
                                    usage["total_tokens"] = sum(usage.values())
                            if usage:
                                if transport.responses:
                                    usage.update(_generation_usage(transport.responses[-1]))
                                yield {"usage": usage}
                            if chunk.tool_call_chunks:
                                call_size += sum(len(part.get("args") or "") for part in chunk.tool_call_chunks)
                                if call_size > 600_000:
                                    raise ProviderError(provider, "tool_call_too_large")
                                part = chunk.model_copy(update={"content": "", "response_metadata": {}, "usage_metadata": None})
                                aggregate = part if aggregate is None else aggregate + part
                if not finished:
                    raise ProviderError(provider, "stream_ended_before_completion")
                if aggregate is not None:
                    # Use assembled raw arguments, not LangChain's tolerant partial
                    # JSON parser; GreatSage validates the complete object later.
                    pending = {index: {"id": item.get("id"), "name": item.get("name"), "arguments": item.get("args")}
                               for index, item in enumerate(aggregate.tool_call_chunks)}
                    yield {"tool_calls": _tool_calls(pending)}
    except ProviderError:
        raise
    except (TimeoutError, httpx.TimeoutException, openai.APITimeoutError):
        raise ProviderError(provider, "timeout") from None
    except openai.APIStatusError as exc:
        service._status(exc.response, provider)
        raise ProviderError(provider, "upstream_error") from None
    except ollama.ResponseError as exc:
        service._status(httpx.Response(exc.status_code or 500), provider)
        raise ProviderError(provider, "upstream_error") from None
    except openai.APIConnectionError as exc:
        code = _network_code(exc.__cause__) if isinstance(exc.__cause__, httpx.HTTPError) else "connection_failed"
        raise ProviderError(provider, code) from None
    except httpx.HTTPError as exc:
        raise ProviderError(provider, _network_code(exc)) from None
    except openai.APIError:
        raise ProviderError(provider, "upstream_error") from None
    except ConnectionError:
        raise ProviderError(provider, "connection_failed") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError(provider, "invalid_json_response") from None
    except (TypeError, ValueError, KeyError):
        raise ProviderError(provider, "invalid_response_shape") from None
